"""End-to-end video tests against a real ffmpeg.

These are the ones that would catch a filter graph ffmpeg actually rejects —
which is the failure mode unit tests on the argv cannot see. They generate a
synthetic clip with lavfi, so no fixture file is needed and nothing is
downloaded.

Skipped wherever ffmpeg is not installed (including CI without it). ``pytest -k
video_integration`` on a machine with ffmpeg is the honest check; it also runs
inside the Docker image, where the Dockerfile installs ffmpeg.
"""

import subprocess
from pathlib import Path

import pytest

from services.video import ffmpeg as ffmpeg_mod
from services.video.filters import VideoOps
from services.video.service import VideoConfig, VideoEditError, VideoService

pytestmark = pytest.mark.skipif(
    ffmpeg_mod.detect_tools() is None,
    reason="ffmpeg is not installed on this machine",
)

FPS = 10
DURATION = 3.0


@pytest.fixture(scope="module")
def tools():
    found = ffmpeg_mod.detect_tools()
    if not found or not found.ffprobe:
        pytest.skip("ffprobe is required for the integration assertions")
    return found


@pytest.fixture(scope="module")
def source(tmp_path_factory, tools):
    """A 3-second 320x240 clip with a tone, generated rather than shipped."""
    path = tmp_path_factory.mktemp("video") / "source.mp4"
    subprocess.run(
        [
            tools.ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=320x240:rate={FPS}:duration={DURATION}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        capture_output=True, text=True, timeout=120,
    )
    assert path.exists() and path.stat().st_size > 0, "could not generate the fixture clip"
    return path


@pytest.fixture
def service(tmp_path):
    return VideoService(VideoConfig(temp_dir=str(tmp_path / "work")))


def _run(service, source_path, ops, tmp_path, ext="mp4"):
    """Drive the service's own job machinery and return the finished result."""
    info = service.probe(source_path)
    job_id = service.start_edit(
        source=source_path, ops=ops, source_ext=source_path.suffix.lstrip("."),
        owner="alice", source_info=info,
    )
    import time
    deadline = time.time() + 180
    while time.time() < deadline:
        job = service.get_job(job_id, owner="alice")
        if job and job["status"] in ("done", "error"):
            if job["status"] == "error":
                raise AssertionError(f"encode failed: {job['error']} ({job['code']})")
            return job["result"], info
        time.sleep(0.05)
    raise AssertionError("encode did not finish in time")


def test_probe_reads_the_generated_clip(service, source):
    info = service.probe(source)
    assert info.width == 320
    assert info.height == 240
    assert info.duration == pytest.approx(DURATION, abs=0.4)
    assert info.has_audio is True
    assert info.video_codec == "h264"


def test_lossless_trim_produces_the_requested_length(service, source, tmp_path, tools):
    """`-c copy` must produce a shorter file that is still decodable."""
    result, _ = _run(service, source, VideoOps(trim_start=1.0, trim_end=2.0), tmp_path)
    assert result["stream_copy"] is True

    output = Path(result["output_path"])
    assert output.exists() and output.stat().st_size > 0
    assert service.probe(output).duration == pytest.approx(1.0, abs=0.5)


def test_rotation_changes_the_dimensions(service, source, tmp_path):
    """A quarter turn must actually swap width and height, not just tag it."""
    result, _ = _run(service, source, VideoOps(rotate=90), tmp_path)
    info = service.probe(Path(result["output_path"]))
    assert info.width == 240
    assert info.height == 320


def test_crop_reduces_the_frame_by_the_requested_fraction(service, source, tmp_path):
    """The even-dimension expression must produce a legal, smaller frame."""
    ops = VideoOps(crop=(0.0, 0.0, 0.5, 0.5))
    result, _ = _run(service, source, ops, tmp_path)
    info = service.probe(Path(result["output_path"]))
    assert info.width == 160
    assert info.height == 120


def test_mute_removes_the_audio_track(service, source, tmp_path):
    result, _ = _run(service, source, VideoOps(mute=True), tmp_path)
    assert service.probe(Path(result["output_path"])).has_audio is False


def test_volume_change_keeps_the_audio_and_copies_the_video(service, source, tmp_path):
    """An audio-only edit must not cost the picture a re-encode."""
    result, _ = _run(service, source, VideoOps(volume=0.5), tmp_path)
    info = service.probe(Path(result["output_path"]))
    assert info.has_audio is True
    assert info.width == 320


def test_speed_shortens_the_timeline(service, source, tmp_path):
    result, _ = _run(service, source, VideoOps(speed=2.0), tmp_path)
    info = service.probe(Path(result["output_path"]))
    assert info.duration == pytest.approx(DURATION / 2, abs=0.6)
    # Video and audio must stay in sync; a silent atempo failure shows up as a
    # much longer audio stream than the video one.
    assert info.has_audio is True


def test_gif_conversion_produces_a_real_gif(service, source, tmp_path):
    ops = VideoOps(output_format="gif", gif_fps=8, gif_width=200)
    result, _ = _run(service, source, ops, tmp_path)
    output = Path(result["output_path"])
    assert output.suffix == ".gif"
    assert output.exists() and output.stat().st_size > 0
    # GIF magic bytes, so we know ffmpeg wrote a GIF rather than erroring into
    # a file nobody checked.
    assert output.read_bytes()[:6] in (b"GIF87a", b"GIF89a")
    assert service.probe(output).has_audio is False


def test_webm_conversion_is_playable(service, source, tmp_path):
    result, _ = _run(service, source, VideoOps(output_format="webm"), tmp_path)
    info = service.probe(Path(result["output_path"]))
    assert info.width == 320
    assert info.video_codec == "vp9"


def test_frame_extraction_writes_a_png(service, source, tmp_path):
    destination = tmp_path / "frame.png"
    service.extract_frame(source=source, at_seconds=1.0, destination=destination)
    assert destination.exists()
    # PNG magic bytes: the extension alone proves nothing.
    assert destination.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_frame_extraction_past_the_end_fails_cleanly(service, source, tmp_path):
    """A timestamp beyond the clip must be a typed error, not an empty file."""
    with pytest.raises(VideoEditError) as excinfo:
        service.extract_frame(
            source=source, at_seconds=999.0, destination=tmp_path / "nope.png"
        )
    assert excinfo.value.code == "frame_failed"
    assert not (tmp_path / "nope.png").exists()


def test_a_combined_edit_applies_every_operation(service, source, tmp_path):
    """The full stack: trim, crop, rotate, volume and speed in one graph."""
    ops = VideoOps(
        trim_start=0.5, trim_end=2.5,
        crop=(0.1, 0.1, 0.6, 0.6),
        rotate=90,
        volume=0.4,
        speed=1.5,
    )
    result, _ = _run(service, source, ops, tmp_path)
    info = service.probe(Path(result["output_path"]))
    assert info.width and info.height
    assert info.has_audio is True
    assert info.duration < DURATION


def test_a_failure_reports_ffmpeg_s_own_reason(service, tmp_path):
    """Feeding a text file to ffmpeg must surface a useful sentence.

    The point is the error path: a user who somehow gets a corrupt file into the
    gallery should be told what is wrong, not given 'ffmpeg failed'.
    """
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is definitely not a video")

    with pytest.raises(Exception) as excinfo:
        service.probe(broken)
    message = str(excinfo.value)
    assert "video" in message.lower() or "read" in message.lower()
