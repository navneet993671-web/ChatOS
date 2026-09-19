"""ffmpeg/ffprobe discovery, metadata probing, and safe process execution.

Why detection instead of a hard requirement: Misantropic is local-first and
expects to run on machines where ffmpeg may simply not be installed. Editing is
therefore a *capability* the API reports, not an assumption it makes. When the
binary is missing the routes refuse cleanly and the browser falls back to its
own encoder, so the feature degrades instead of the gallery breaking.

Two boundaries this module holds:

* **No shell.** Commands are argv lists handed to ``subprocess`` with
  ``shell=False``. Filenames come from the database and could contain anything,
  so no argument is ever interpolated into a shell string.
* **Bounded.** Every invocation has a timeout and a capped output tail, so a
  pathological file cannot pin the server indefinitely or exhaust memory with
  ffmpeg's very chatty stderr.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Explicit overrides win over PATH, which is what makes a non-standard install
# (or a bundled binary) usable without touching the system environment.
FFMPEG_ENV_VAR = "MISANTROPIC_FFMPEG"
FFPROBE_ENV_VAR = "MISANTROPIC_FFPROBE"

# Kept deliberately small: a large tail here is only ever shown to a human when
# an encode fails, and ffmpeg can emit megabytes of per-frame noise.
_MAX_DIAGNOSTIC_CHARS = 4000


class FfmpegUnavailable(RuntimeError):
    """Raised when an operation needs a binary that is not installed."""


class FfmpegFailed(RuntimeError):
    """Raised when ffmpeg exits non-zero."""

    def __init__(self, message: str, *, returncode: int = -1, output: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.output = output


class FfmpegTimeout(FfmpegFailed):
    """Raised when an encode exceeds its timeout."""


@dataclass(frozen=True)
class VideoInfo:
    """What ffprobe reports about a file, normalised to what the UI needs."""

    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    video_codec: str = ""
    audio_codec: str = ""
    container: str = ""
    size_bytes: int = 0

    def as_dict(self) -> Dict[str, object]:
        return {
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "has_audio": self.has_audio,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "container": self.container,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class FfmpegTools:
    """Located binaries. ``ffprobe`` is optional: edits work without it, only
    metadata probing degrades."""

    ffmpeg: str
    ffprobe: Optional[str] = None
    version: str = ""


def _binary_name(stem: str) -> str:
    return f"{stem}.exe" if os.name == "nt" else stem


def _candidate_paths(stem: str) -> List[str]:
    """Conventional install locations, tried after PATH.

    These cover the ways people actually install ffmpeg on each platform
    (winget, scoop, Homebrew, distro packages, snap) so the common case needs no
    configuration at all.
    """
    name = _binary_name(stem)
    paths = [
        f"/opt/homebrew/bin/{name}",        # macOS (Apple Silicon)
        f"/usr/local/bin/{name}",           # macOS (Intel), manual Linux installs
        f"/usr/bin/{name}",                 # distro packages
        f"/snap/bin/{name}",                # snap
        f"C:/ffmpeg/bin/{name}",            # common manual Windows install
        f"C:/Program Files/ffmpeg/bin/{name}",
    ]
    home = Path.home()
    paths += [
        str(home / "scoop" / "shims" / name),
        str(home / "scoop" / "apps" / "ffmpeg" / "current" / "bin" / name),
    ]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        # winget installs shims here rather than on PATH for some shells.
        paths.append(str(Path(local_appdata) / "Microsoft" / "WinGet" / "Links" / name))
    return paths


def find_binary(stem: str, env_var: str) -> Optional[str]:
    """Locate ``stem`` via the explicit override, then PATH, then conventions.

    An override that points at a missing file falls through rather than
    failing, so a stale value in ``.env`` cannot disable a working install.
    """
    override = (os.environ.get(env_var) or "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return str(candidate)
        logger.warning("%s=%r is not a file; falling back to PATH", env_var, override)

    found = shutil.which(stem)
    if found:
        return found

    for candidate in _candidate_paths(stem):
        if Path(candidate).is_file():
            return candidate
    return None


_tools_cache: Optional[FfmpegTools] = None
_tools_lock = threading.Lock()


def detect_tools(*, refresh: bool = False) -> Optional[FfmpegTools]:
    """Find ffmpeg (and ffprobe), caching the result.

    Cached because this runs while building API responses and PATH lookups are
    not free. ``refresh=True`` re-probes — used by ``GET /api/video/capabilities``
    so a user who installs ffmpeg while the server is running sees it appear
    without a restart.
    """
    global _tools_cache
    with _tools_lock:
        if _tools_cache is not None and not refresh:
            return _tools_cache

        ffmpeg = find_binary("ffmpeg", FFMPEG_ENV_VAR)
        if not ffmpeg:
            _tools_cache = None
            return None

        ffprobe = find_binary("ffprobe", FFPROBE_ENV_VAR)
        tools = FfmpegTools(
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            version=_read_version(ffmpeg),
        )
        _tools_cache = tools
        logger.info(
            "ffmpeg detected: %s (version %s, ffprobe %s)",
            tools.ffmpeg, tools.version or "unknown",
            "yes" if tools.ffprobe else "no",
        )
        return tools


def reset_tools_cache() -> None:
    """Drop the cached detection result (used by tests and by refresh calls)."""
    global _tools_cache
    with _tools_lock:
        _tools_cache = None


def _read_version(ffmpeg: str) -> str:
    try:
        proc = subprocess.run(
            [ffmpeg, "-version"],
            capture_output=True, text=True, timeout=10,
            # CREATE_NO_WINDOW: without it a GUI-less Windows app flashes a
            # console window on every probe.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        first = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
        # "ffmpeg version 6.1.1 Copyright (c) ..." -> "6.1.1"
        parts = first.split()
        return parts[2] if len(parts) > 2 else first
    except Exception:
        return ""


def parse_probe_json(payload: Dict[str, object]) -> VideoInfo:
    """Normalise ``ffprobe -print_format json`` output.

    Pure and defensive: streams/codec fields are optional and ``duration`` is
    absent for some containers, in which case 0.0 is reported rather than a
    crash. ``r_frame_rate`` arrives as the rational string ``"30000/1001"``.
    """
    streams = payload.get("streams") or []
    if not isinstance(streams, list):
        streams = []

    video = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"),
        {},
    )
    audio = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"),
        None,
    )
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}

    def _int(value: object) -> int:
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return 0

    def _float(value: object) -> float:
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return 0.0

    fps = 0.0
    rate = video.get("r_frame_rate") or video.get("avg_frame_rate") or ""
    if "/" in str(rate):
        num, _, den = str(rate).partition("/")
        denominator = _float(den)
        if denominator:
            fps = _float(num) / denominator
    elif rate:
        fps = _float(rate)

    # duration lives on the format for most containers, but a stream may carry
    # it when the format does not (and vice versa).
    duration = _float(fmt.get("duration") or video.get("duration"))

    return VideoInfo(
        duration=round(duration, 3),
        width=_int(video.get("width")),
        height=_int(video.get("height")),
        fps=round(fps, 3),
        has_audio=audio is not None,
        video_codec=str(video.get("codec_name") or ""),
        audio_codec=str((audio or {}).get("codec_name") or ""),
        container=str(fmt.get("format_name") or ""),
        size_bytes=_int(fmt.get("size")),
    )


def probe(path: Path, tools: Optional[FfmpegTools] = None) -> VideoInfo:
    """Read metadata for ``path``. Raises ``FfmpegUnavailable`` without ffprobe."""
    tools = tools or detect_tools()
    if not tools:
        raise FfmpegUnavailable("ffmpeg is not installed")
    if not tools.ffprobe:
        raise FfmpegUnavailable("ffprobe is not installed")

    proc = subprocess.run(
        [
            tools.ffprobe,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise FfmpegFailed(
            "ffprobe could not read this file",
            returncode=proc.returncode,
            output=(proc.stderr or "")[-_MAX_DIAGNOSTIC_CHARS:],
        )
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        raise FfmpegFailed("ffprobe returned unreadable output", output=proc.stdout or "")
    return parse_probe_json(payload if isinstance(payload, dict) else {})


def _parse_progress_line(line: str) -> Optional[float]:
    """Seconds completed from one ``-progress`` line, else ``None``.

    ffmpeg emits ``out_time_ms`` but the value is microseconds — a long-standing
    misnomer kept for compatibility. ``out_time_us`` is the honest alias, so it
    is preferred when present.
    """
    key, _, raw = line.partition("=")
    key = key.strip()
    if key not in ("out_time_us", "out_time_ms"):
        return None
    raw = raw.strip()
    if not raw or raw == "N/A":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < 0:
        return None
    return value / 1_000_000.0


def with_progress_flags(args: Sequence[str]) -> List[str]:
    """Append ffmpeg's machine-readable progress output to an argv.

    ``-progress pipe:1`` emits ``key=value`` lines (including ``out_time_us``)
    instead of the human-readable status line, and ``-nostats`` stops ffmpeg
    writing that status line over the top of them.
    """
    return [*args, "-progress", "pipe:1", "-nostats"]


def run_ffmpeg(
    args: List[str],
    *,
    timeout: float,
    total_seconds: Optional[float] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> None:
    """Run an argv list, optionally reporting progress as a 0..1 fraction.

    stdout and stderr are merged into a single pipe on purpose: ffmpeg's stderr
    is extremely verbose, and two pipes that nobody drains will deadlock the
    child once a buffer fills. One pipe with a parseable progress format cannot
    deadlock, and non-progress lines are simply ignored.
    """
    argv = with_progress_flags(args)
    tail: List[str] = []

    def _spawn() -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    try:
        proc = _spawn()
    except subprocess.TimeoutExpired as exc:
        raise FfmpegTimeout(
            f"ffmpeg exceeded its {int(timeout)}s timeout",
            output=str(exc.output or "")[-_MAX_DIAGNOSTIC_CHARS:],
        )

    output = proc.stdout or ""
    if output:
        tail = output.splitlines()[-80:]

    # run() has already finished by now, so progress is reported once from the
    # captured output. Streaming callbacks live in run_ffmpeg_streaming below;
    # this keeps the common (fast) path simple and deadlock-free.
    if on_progress and total_seconds and total_seconds > 0 and output:
        for line in output.splitlines():
            seconds = _parse_progress_line(line)
            if seconds is not None:
                on_progress(max(0.0, min(1.0, seconds / total_seconds)))
        on_progress(1.0)

    if proc.returncode != 0:
        diagnostic = "\n".join(tail)[-_MAX_DIAGNOSTIC_CHARS:]
        raise FfmpegFailed(
            _summarise_failure(diagnostic),
            returncode=proc.returncode,
            output=diagnostic,
        )


def _summarise_failure(diagnostic: str) -> str:
    """Pull ffmpeg's actual complaint out of its log spew.

    ffmpeg prints the reason on one of the last stderr lines (often prefixed
    ``[Error]``, ``Error``, or ``Invalid``), but the very last line is usually
    a generic trailer. Surfacing the real sentence is the difference between a
    usable error and "ffmpeg failed".
    """
    interesting = [
        line.strip()
        for line in diagnostic.splitlines()
        if line.strip() and not line.strip().startswith("frame=")
    ]
    for line in reversed(interesting):
        lowered = line.lower()
        if any(marker in lowered for marker in ("error", "invalid", "unable", "no such", "not found", "denied")):
            return line[:300]
    return interesting[-1][:300] if interesting else "ffmpeg failed"


def run_ffmpeg_streaming(
    args: List[str],
    *,
    timeout: float,
    total_seconds: Optional[float] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> None:
    """Like ``run_ffmpeg`` but reports progress while the process runs.

    Used for real encodes, where a long job needs a live progress bar. A
    dedicated reader thread drains the pipe so the child can never block, and
    the timeout is enforced by ``communicate``'s own deadline plus an explicit
    kill in the ``finally``.
    """
    argv = with_progress_flags(args)
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    tail: List[str] = []
    last_error: List[str] = []

    def _reader() -> None:
        try:
            for line in proc.stdout or []:
                tail.append(line.rstrip())
                if len(tail) > 200:
                    del tail[:-200]
                stripped = line.strip()
                if stripped.startswith(("Error", "[Error]", "Invalid", "Unable", "No such")):
                    last_error.append(stripped)
                seconds = _parse_progress_line(line)
                if seconds is not None and on_progress and total_seconds and total_seconds > 0:
                    on_progress(max(0.0, min(0.999, seconds / total_seconds)))
        except Exception:  # pragma: no cover - defensive, pipe closed early
            pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
        raise FfmpegTimeout(
            f"ffmpeg exceeded its {int(timeout)}s timeout",
            output="\n".join(tail)[-_MAX_DIAGNOSTIC_CHARS:],
        )
    finally:
        reader.join(timeout=5)

    diagnostic = "\n".join(tail)[-_MAX_DIAGNOSTIC_CHARS:]
    if proc.returncode != 0:
        reason = last_error[-1] if last_error else _summarise_failure(diagnostic)
        raise FfmpegFailed(reason, returncode=proc.returncode, output=diagnostic)

    if on_progress:
        on_progress(1.0)


def install_hint() -> str:
    """Platform-appropriate one-liner for the UI when ffmpeg is missing."""
    if os.name == "nt":
        return "Install it with: winget install Gyan.FFmpeg  (then refresh this page)"
    if sys.platform == "darwin":
        return "Install it with: brew install ffmpeg  (then refresh this page)"
    return "Install it with: sudo apt install ffmpeg  (then refresh this page)"
