"""Pure filter-graph and argv construction for video edits.

Everything in this module is a function of its arguments: no filesystem, no
subprocess, no ffmpeg binary. That is deliberate. The bugs that actually bite
in a video editor are wrong filter *order*, off-by-one trim maths, and odd
crop dimensions that h264 refuses to encode — all of which are decidable in
process, and all of which are tested in ``tests/test_video_filters.py`` on a
machine that has no ffmpeg at all.

Filter order is fixed and meaningful:

    crop -> rotate -> flip -> speed -> [gif: fps, scale, palette]

Crop runs first because the user drew their box against the *source* frame
they were looking at. Speed runs late so ``setpts`` rewrites the timeline
after geometry is settled. GIF's frame-rate reduction runs *after* ``setpts``
so the exported frame count reflects the sped-up timeline rather than the
original one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# Formats we can write. "same" means "keep whatever the source container was",
# which is what makes the lossless trim fast path possible.
VIDEO_FORMATS: Tuple[str, ...] = ("same", "mp4", "webm", "gif", "mkv")

# Container -> the format family we encode for. ``mov``/``m4v`` are mp4 in all
# but name, and ``mkv`` shares the mp4 encoder set.
_CONTAINER_FAMILY = {
    "mp4": "mp4",
    "m4v": "mp4",
    "mov": "mp4",
    "mkv": "mkv",
    "webm": "webm",
    "gif": "gif",
}

# h264/VP9 both want planar 4:2:0 chroma, which also needs even dimensions.
# Appending this is a no-op on already-even frames and rescues odd ones.
_EVEN_SCALE = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

_ATEMPO_MIN = 0.5
_ATEMPO_MAX = 2.0


class VideoOpsError(ValueError):
    """An edit request that cannot be expressed as a valid ffmpeg command."""


@dataclass(frozen=True)
class VideoOps:
    """A normalised, already-validated set of operations.

    Values are clamped/checked by ``services.video.schemas`` before they reach
    here, but the dataclass still asserts its own invariants because it is also
    constructed directly in tests and by the browser-fallback comparison.
    """

    trim_start: float = 0.0
    trim_end: Optional[float] = None
    rotate: int = 0
    flip_h: bool = False
    flip_v: bool = False
    # (x, y, width, height) as 0..1 fractions of the source frame, so a crop
    # survives a re-encode at a different resolution.
    crop: Optional[Tuple[float, float, float, float]] = None
    mute: bool = False
    volume: float = 1.0
    speed: float = 1.0
    output_format: str = "same"
    gif_fps: int = 12
    gif_width: int = 480

    @property
    def trim_duration(self) -> Optional[float]:
        """Length of the kept range, or ``None`` for "to the end"."""
        if self.trim_end is None:
            return None
        return max(0.0, self.trim_end - self.trim_start)

    @property
    def has_geometry_ops(self) -> bool:
        return bool(self.crop) or self.rotate != 0 or self.flip_h or self.flip_v

    @property
    def has_video_ops(self) -> bool:
        """True when the video *stream itself* must be re-encoded."""
        return self.has_geometry_ops or abs(self.speed - 1.0) > 1e-9

    @property
    def has_audio_ops(self) -> bool:
        """True when the audio stream must be re-encoded (not merely dropped)."""
        if self.mute:
            return False  # dropped entirely, which a stream copy can also do
        return abs(self.speed - 1.0) > 1e-9 or abs(self.volume - 1.0) > 1e-9


def container_family(ext: str) -> str:
    """Map a file extension to its encoder family (``mp4`` for mov/m4v)."""
    return _CONTAINER_FAMILY.get((ext or "").lower().lstrip("."), "mp4")


def resolve_container(source_ext: str, output_format: str) -> str:
    """The concrete container to write, resolving ``"same"`` against the source."""
    fmt = (output_format or "same").lower()
    if fmt == "same":
        return container_family(source_ext)
    if fmt not in _CONTAINER_FAMILY:
        raise VideoOpsError(f"Unsupported output format: {output_format!r}")
    return _CONTAINER_FAMILY[fmt]


def atempo_chain(speed: float) -> List[str]:
    """Build the ``atempo`` chain needed for an arbitrary speed factor.

    A single ``atempo`` only accepts 0.5–2.0 on older ffmpeg builds, so
    anything outside that range is reached by chaining instances (2x then 2x
    for 4x, and so on). Ordering does not matter because the operation is
    multiplicative, which is why this always emits the coarse factors first.
    """
    if abs(speed - 1.0) <= 1e-9:
        return []
    if speed <= 0:
        raise VideoOpsError("speed must be greater than 0")

    parts: List[str] = []
    remaining = float(speed)
    while remaining > _ATEMPO_MAX:
        parts.append(f"atempo={_ATEMPO_MAX}")
        remaining /= _ATEMPO_MAX
    while remaining < _ATEMPO_MIN:
        parts.append(f"atempo={_ATEMPO_MIN}")
        remaining /= _ATEMPO_MIN
    if abs(remaining - 1.0) > 1e-9:
        parts.append(f"atempo={remaining:.6f}")
    return parts


def build_video_filters(ops: VideoOps, *, container: str) -> List[str]:
    """Ordered video filter chain for ``ops`` (may be empty)."""
    chain: List[str] = []

    if ops.crop:
        x, y, w, h = ops.crop
        # Even dimensions *and* even offsets in one expression. h264/yuv420p
        # rejects odd crop offsets, and doing it here avoids a second `scale`
        # pass just to fix a one-pixel problem. `crop=W:H:X:Y`.
        chain.append(
            "crop="
            f"floor(iw*{w:.6f}/2)*2:"
            f"floor(ih*{h:.6f}/2)*2:"
            f"floor(iw*{x:.6f}/2)*2:"
            f"floor(ih*{y:.6f}/2)*2"
        )

    if ops.rotate == 90:
        chain.append("transpose=1")  # clockwise
    elif ops.rotate == 180:
        chain.append("hflip")
        chain.append("vflip")
    elif ops.rotate == 270:
        chain.append("transpose=2")  # counter-clockwise
    elif ops.rotate != 0:
        raise VideoOpsError("rotate must be one of 0, 90, 180, 270")

    if ops.flip_h:
        chain.append("hflip")
    if ops.flip_v:
        chain.append("vflip")

    if abs(ops.speed - 1.0) > 1e-9:
        chain.append(f"setpts=PTS/{ops.speed:.6f}")

    if container == "gif":
        # Palettised single pass: reduce frame rate, scale down, then build a
        # palette from the stream and re-map it. Doing this in one graph avoids
        # a second ffmpeg invocation and a temp PNG.
        width = max(2, ops.gif_width - (ops.gif_width % 2))
        chain.append(f"fps={max(1, ops.gif_fps)}")
        chain.append(f"scale={width}:-1:flags=lanczos")
        chain.append("split[s0][s1];[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer")
    elif chain:
        # Only planar formats need even dimensions, and only when we are
        # actually rewriting the video stream.
        chain.append(_EVEN_SCALE)

    return chain


def build_audio_filters(ops: VideoOps) -> List[str]:
    """Ordered audio filter chain for ``ops`` (may be empty)."""
    chain: List[str] = []
    if ops.mute:
        return chain
    if abs(ops.volume - 1.0) > 1e-9:
        chain.append(f"volume={ops.volume:.4f}")
    chain.extend(atempo_chain(ops.speed))
    return chain


def can_stream_copy(ops: VideoOps, source_ext: str) -> bool:
    """True when the edit reduces to dropping frames, so ``-c copy`` applies.

    This is the difference between a 30 ms trim and a two-minute re-encode, and
    it covers the single most common request ("just cut this bit off"). Muting
    still qualifies: dropping an audio stream needs no encoder.
    """
    if not ops.has_video_ops and not ops.has_audio_ops:
        container = resolve_container(source_ext, ops.output_format)
        if container == "gif":
            return False
        # Stream copy cannot change container, so the target must match.
        return container == container_family(source_ext)
    return False


def build_video_encoder_args(container: str) -> List[str]:
    """Video-stream codec/quality arguments for the resolved container."""
    if container in ("mp4", "mkv"):
        args = [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
        ]
        if container == "mp4":
            # Put the moov atom first so the file streams/scrubs immediately.
            args += ["-movflags", "+faststart"]
        return args
    if container == "webm":
        return [
            "-c:v", "libvpx-vp9",
            "-crf", "32",
            "-b:v", "0",
            "-row-mt", "1",
            "-deadline", "good",
            "-cpu-used", "3",
            "-pix_fmt", "yuv420p",
        ]
    if container == "gif":
        return ["-c:v", "gif"]
    raise VideoOpsError(f"Unsupported container: {container!r}")


def build_audio_encoder_args(
    container: str, *, has_audio: bool, mute: bool
) -> List[str]:
    """Audio-stream arguments, including the decision to drop audio entirely."""
    if container == "gif" or mute or not has_audio:
        return ["-an"]
    if container in ("mp4", "mkv"):
        return ["-c:a", "aac", "-b:a", "160k"]
    if container == "webm":
        return ["-c:a", "libopus", "-b:a", "128k"]
    raise VideoOpsError(f"Unsupported container: {container!r}")


def build_encoder_args(container: str, *, has_audio: bool, mute: bool) -> List[str]:
    """Both streams' arguments, for callers that are re-encoding everything."""
    return build_video_encoder_args(container) + build_audio_encoder_args(
        container, has_audio=has_audio, mute=mute
    )


def build_edit_args(
    *,
    ffmpeg: str,
    source: str,
    destination: str,
    ops: VideoOps,
    source_ext: str,
    source_has_audio: bool,
    stream_copy: bool = False,
) -> List[str]:
    """Full argv for one edit.

    ``-ss`` is placed *before* ``-i`` (input seeking), which is what makes a
    trim on a long file fast — ffmpeg seeks rather than decoding from zero.
    ``-t`` is then used for the *duration* rather than ``-to``, because ``-to``
    is interpreted against the post-seek timeline and silently produces the
    wrong range when the two are mixed up.
    """
    container = resolve_container(source_ext, ops.output_format)
    duration = ops.trim_duration

    args: List[str] = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
    if ops.trim_start > 0:
        args += ["-ss", f"{ops.trim_start:.3f}"]
    args += ["-i", str(source)]
    if duration is not None:
        args += ["-t", f"{duration:.3f}"]

    # Keep the first video stream only; drop subtitles/attachments/data so a
    # weird source container cannot smuggle streams into the output.
    args += ["-map", "0:v:0", "-sn", "-dn"]

    if stream_copy:
        args += ["-map", "0:a:0?"]
        args += ["-c:v", "copy", "-c:a", "copy"]
        if ops.mute or not source_has_audio:
            args += ["-an"]
        args += [str(destination)]
        return args

    video_filters = build_video_filters(ops, container=container)
    if video_filters:
        args += ["-vf", ",".join(video_filters)]

    # An audio filter chain is only valid when audio is actually being encoded.
    # Emitting `-af` alongside `-an` (muted, or a source with no audio track)
    # makes ffmpeg fail the whole run, so the two must be decided together.
    # GIF carries no audio at all, so its chain would be dead weight regardless.
    audio_filters = (
        []
        if (container == "gif" or ops.mute or not source_has_audio)
        else build_audio_filters(ops)
    )
    if audio_filters:
        args += ["-af", ",".join(audio_filters)]

    if container == "gif":
        # Loop forever, like every other animated GIF.
        args += build_video_encoder_args(container)
        args += build_audio_encoder_args(
            container, has_audio=source_has_audio, mute=True
        )
        args += ["-loop", "0", str(destination)]
        return args

    # An audio-only edit (volume or speed) should NOT cost the video a
    # generation of quality. Copying the video stream is only legal when the
    # container is unchanged, because a codec valid in one container (h264 in
    # mp4) may be illegal in another (webm).
    copy_video = (
        not ops.has_video_ops and container == container_family(source_ext)
    )
    if copy_video:
        args += ["-c:v", "copy"]
    else:
        args += build_video_encoder_args(container)

    args += build_audio_encoder_args(
        container, has_audio=source_has_audio, mute=ops.mute
    )
    args += [str(destination)]
    return args


def build_frame_args(
    *,
    ffmpeg: str,
    source: str,
    destination: str,
    at_seconds: float,
    width: Optional[int] = None,
) -> List[str]:
    """argv that writes a single frame as a PNG.

    ``-ss`` before ``-i`` again, then exactly one frame. ``-frames:v 1`` rather
    than ``-vframes 1`` because it is the current spelling and survives
    deprecation, and no audio is mapped at all.
    """
    args: List[str] = [
        ffmpeg, "-hide_banner", "-nostdin", "-y",
        "-ss", f"{max(0.0, at_seconds):.3f}",
        "-i", str(source),
        "-map", "0:v:0",
        "-frames:v", "1",
    ]
    if width:
        safe_width = max(2, width - (width % 2))
        args += ["-vf", f"scale={safe_width}:-2:flags=lanczos"]
    args += ["-f", "image2", "-c:v", "png", str(destination)]
    return args
