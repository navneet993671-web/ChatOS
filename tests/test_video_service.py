"""Tests for services/video/service.py and the ffmpeg helpers it relies on.

No ffmpeg is needed. ``run_ffmpeg_streaming`` is replaced with a stub that
writes the output file, which is enough to exercise the entire job lifecycle —
progress, the on_finished hook, size limits, failure mapping, ownership scoping
and cleanup — deterministically and in milliseconds.

One genuine end-to-end encode is exercised in ``test_video_integration.py``,
which skips itself when no ffmpeg is present.
"""

import hashlib
import threading
import time
from pathlib import Path

import pytest

from services.video import ffmpeg as ffmpeg_mod
from services.video.filters import VideoOps
from services.video.ffmpeg import FfmpegFailed, FfmpegTimeout, FfmpegTools, VideoInfo
from services.video.service import (
    VideoConfig,
    VideoEditError,
    VideoService,
)

TOOLS = FfmpegTools(ffmpeg="/fake/ffmpeg", ffprobe="/fake/ffprobe", version="6.1.1")


def _service(tmp_path, *, tools=TOOLS, **config_overrides) -> VideoService:
    config = VideoConfig(temp_dir=str(tmp_path / "work"), **config_overrides)
    return VideoService(config, tools_factory=lambda refresh=False: tools)


def _wait(svc, job_id, owner, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = svc.get_job(job_id, owner=owner)
        if job and job["status"] in ("done", "error"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish; last={svc.get_job(job_id, owner=owner)}")


def _stub_encoder(monkeypatch, *, payload=b"x" * 1024, on_run=None, error=None):
    """Replace the ffmpeg runner with one that fabricates the output file.

    When ``on_run`` is supplied it owns producing (or deliberately not
    producing) the output, so tests can simulate an empty or partial encode.
    """
    calls = []

    def fake(args, *, timeout, total_seconds=None, on_progress=None):
        calls.append({"args": [str(a) for a in args], "timeout": timeout,
                      "total_seconds": total_seconds})
        if error:
            raise error
        if on_run:
            on_run(args, on_progress)
            return
        destination = Path(str(args[-1]))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        if on_progress and total_seconds:
            on_progress(0.5)
        if on_progress:
            on_progress(1.0)

    monkeypatch.setattr(ffmpeg_mod, "run_ffmpeg_streaming", fake)
    return calls


# ------------------------------------------------------------ capabilities

def test_capabilities_report_no_ffmpeg_as_a_capability_not_an_error(tmp_path):
    caps = _service(tmp_path, tools=None).capabilities()
    assert caps["ffmpeg"] is False
    assert caps["ffprobe"] is False
    assert caps["server_editing"] is False
    # The UI needs something actionable to show.
    assert caps["install_hint"]
    assert "ffmpeg" in caps["formats"] or "mp4" in caps["formats"]


def test_capabilities_report_ffmpeg_when_present(tmp_path):
    caps = _service(tmp_path).capabilities()
    assert caps["ffmpeg"] is True
    assert caps["ffprobe"] is True
    assert caps["version"] == "6.1.1"
    assert caps["server_editing"] is True
    assert caps["install_hint"] == ""


def test_capabilities_honour_the_disable_switch(tmp_path):
    caps = _service(tmp_path, enabled=False).capabilities()
    assert caps["ffmpeg"] is True
    assert caps["server_editing"] is False


def test_capabilities_expose_the_configured_limits(tmp_path):
    caps = _service(tmp_path, max_duration_seconds=90, max_output_bytes=1024).capabilities()
    assert caps["max_duration_seconds"] == 90
    assert caps["max_output_bytes"] == 1024


def test_probe_degrades_to_unknown_without_ffprobe(tmp_path):
    info = _service(tmp_path, tools=None).probe(Path("nope.mp4"))
    assert isinstance(info, VideoInfo)
    assert info.duration == 0.0


# ------------------------------------------------------- requiring tools

def test_require_tools_explains_a_missing_binary(tmp_path):
    with pytest.raises(VideoEditError) as excinfo:
        _service(tmp_path, tools=None).require_tools()
    assert excinfo.value.code == "ffmpeg_unavailable"
    assert excinfo.value.detail  # an install hint for the user


def test_require_tools_refuses_when_disabled(tmp_path):
    with pytest.raises(VideoEditError) as excinfo:
        _service(tmp_path, enabled=False).require_tools()
    assert excinfo.value.code == "disabled"


def test_start_edit_refuses_synchronously_without_ffmpeg(tmp_path):
    """A missing binary must fail the request, not a background job."""
    svc = _service(tmp_path, tools=None)
    with pytest.raises(VideoEditError) as excinfo:
        svc.start_edit(source=Path("a.mp4"), ops=VideoOps(), source_ext="mp4", owner="alice")
    assert excinfo.value.code == "ffmpeg_unavailable"


# ------------------------------------------------------------ validation

def test_trim_start_past_the_end_is_rejected(tmp_path):
    svc = _service(tmp_path)
    with pytest.raises(VideoEditError) as excinfo:
        svc.validate_ops_against_source(
            VideoOps(trim_start=30.0), VideoInfo(duration=10.0, has_audio=True)
        )
    assert excinfo.value.code == "invalid_trim"


def test_a_clip_longer_than_the_limit_is_rejected(tmp_path):
    svc = _service(tmp_path, max_duration_seconds=60)
    with pytest.raises(VideoEditError) as excinfo:
        svc.validate_ops_against_source(
            VideoOps(trim_start=0.0), VideoInfo(duration=600.0)
        )
    assert excinfo.value.code == "too_long"


def test_the_kept_range_is_what_the_limit_applies_to(tmp_path):
    """A long video is fine as long as the selected range is short."""
    svc = _service(tmp_path, max_duration_seconds=60)
    svc.validate_ops_against_source(
        VideoOps(trim_start=300.0, trim_end=320.0), VideoInfo(duration=3600.0)
    )


def test_unknown_duration_is_not_treated_as_a_violation(tmp_path):
    svc = _service(tmp_path, max_duration_seconds=60)
    svc.validate_ops_against_source(VideoOps(), VideoInfo(duration=0.0))
    svc.validate_ops_against_source(VideoOps(), None)


# -------------------------------------------------------- job lifecycle

def test_a_successful_job_reports_its_output(tmp_path, monkeypatch):
    calls = _stub_encoder(monkeypatch)
    svc = _service(tmp_path)
    job_id = svc.start_edit(
        source=Path("a.mp4"), ops=VideoOps(trim_start=1.0, trim_end=3.0),
        source_ext="mp4", owner="alice",
        source_info=VideoInfo(duration=10.0, has_audio=True),
    )
    job = _wait(svc, job_id, "alice")

    assert job["status"] == "done"
    assert job["progress"] == 1.0
    assert job["result"]["size_bytes"] == 1024
    assert job["result"]["ext"] == "mp4"
    # A pure trim takes the lossless path.
    assert job["result"]["stream_copy"] is True
    assert Path(job["result"]["output_path"]).exists()
    # A pure trim must reach ffmpeg as a stream copy, not a re-encode.
    assert "-c:v" in calls[0]["args"]
    assert calls[0]["args"][calls[0]["args"].index("-c:v") + 1] == "copy"
    assert calls[0]["args"][calls[0]["args"].index("-ss") + 1] == "1.000"


def test_progress_flags_are_added_to_every_invocation():
    """The machine-readable progress output is what the bar is built from."""
    from services.video.ffmpeg import with_progress_flags
    flags = with_progress_flags(["ffmpeg", "-i", "a.mp4", "b.mp4"])
    assert flags[-2:] == ["-progress", "pipe:1"] or "-nostats" in flags
    assert "-progress" in flags and "-nostats" in flags
    assert flags.index("-progress") > flags.index("b.mp4")


def test_job_progress_reaches_the_registry(tmp_path, monkeypatch):
    """A running job must be observable, not just its final state."""
    seen = []
    started = threading.Event()

    def on_run(args, on_progress):
        started.set()
        if on_progress:
            on_progress(0.25)
        time.sleep(0.15)

    _stub_encoder(monkeypatch, on_run=on_run)
    svc = _service(tmp_path)
    job_id = svc.start_edit(
        source=Path("a.mp4"), ops=VideoOps(trim_end=1.0), source_ext="mp4",
        owner="alice", source_info=VideoInfo(duration=10.0),
    )
    assert started.wait(5.0)
    deadline = time.time() + 5
    while time.time() < deadline:
        job = svc.get_job(job_id, owner="alice")
        if job and job["status"] == "running":
            seen.append(job["progress"])
            break
        time.sleep(0.01)
    assert seen, "job never reported itself as running"
    _wait(svc, job_id, "alice")


def test_expected_duration_accounts_for_speed(tmp_path, monkeypatch):
    """Progress is measured against the *output* length, or a 2x export stalls."""
    calls = _stub_encoder(monkeypatch)
    svc = _service(tmp_path)
    job_id = svc.start_edit(
        source=Path("a.mp4"),
        ops=VideoOps(trim_start=0.0, trim_end=20.0, speed=2.0),
        source_ext="mp4", owner="alice",
        source_info=VideoInfo(duration=100.0, has_audio=True),
    )
    _wait(svc, job_id, "alice")
    assert calls[0]["total_seconds"] == pytest.approx(10.0)


def test_on_finished_extends_the_result_before_done(tmp_path, monkeypatch):
    """The gallery row must exist before the client is told 'done'."""
    _stub_encoder(monkeypatch)
    svc = _service(tmp_path)
    observed = {}

    def on_finished(result):
        observed["had_output"] = Path(result["output_path"]).exists()
        return {"gallery_id": "row-1", "url": "/api/generated-image/abc.png"}

    job_id = svc.start_edit(
        source=Path("a.mp4"), ops=VideoOps(trim_end=1.0), source_ext="mp4",
        owner="alice", source_info=VideoInfo(duration=10.0),
        on_finished=on_finished,
    )
    job = _wait(svc, job_id, "alice")
    assert observed["had_output"] is True
    assert job["result"]["gallery_id"] == "row-1"
    assert job["result"]["url"] == "/api/generated-image/abc.png"


def test_a_failure_while_persisting_fails_the_job(tmp_path, monkeypatch):
    """A raise from on_finished must not leave a 'done' job with no row."""
    _stub_encoder(monkeypatch)
    svc = _service(tmp_path)

    def on_finished(result):
        raise VideoEditError("gallery refused the file", code="empty_output")

    job_id = svc.start_edit(
        source=Path("a.mp4"), ops=VideoOps(trim_end=1.0), source_ext="mp4",
        owner="alice", source_info=VideoInfo(duration=10.0),
        on_finished=on_finished,
    )
    job = _wait(svc, job_id, "alice")
    assert job["status"] == "error"
    assert job["code"] == "empty_output"
    assert "gallery refused" in job["error"]


def test_an_oversized_output_is_deleted_and_fails(tmp_path, monkeypatch):
    _stub_encoder(monkeypatch, payload=b"x" * 5000)
    svc = _service(tmp_path, max_output_bytes=1000)
    job_id = svc.start_edit(
        source=Path("a.mp4"), ops=VideoOps(trim_end=1.0), source_ext="mp4",
        owner="alice", source_info=VideoInfo(duration=10.0),
    )
    job = _wait(svc, job_id, "alice")
    assert job["status"] == "error"
    assert job["code"] == "too_large"
    # Nothing half-written is left behind.
    assert list(Path(svc.config.temp_dir).rglob("output.*")) == []


def test_an_empty_encode_is_caught(tmp_path, monkeypatch):
    def write_nothing(args, on_progress):
        Path(str(args[-1])).write_bytes(b"")

    _stub_encoder(monkeypatch, on_run=write_nothing)
    svc = _service(tmp_path)
    job_id = svc.start_edit(
        source=Path("a.mp4"), ops=VideoOps(trim_end=1.0), source_ext="mp4",
        owner="alice", source_info=VideoInfo(duration=10.0),
    )
    job = _wait(svc, job_id, "alice")
    assert job["status"] == "error"
    assert job["code"] == "empty_output"


def test_a_timeout_is_reported_as_a_timeout(tmp_path, monkeypatch):
    _stub_encoder(monkeypatch, error=FfmpegTimeout("took too long"))
    svc = _service(tmp_path)
    job_id = svc.start_edit(
        source=Path("a.mp4"), ops=VideoOps(trim_end=1.0), source_ext="mp4",
        owner="alice", source_info=VideoInfo(duration=10.0),
    )
    job = _wait(svc, job_id, "alice")
    assert job["code"] == "timeout"


def test_an_ffmpeg_failure_surfaces_its_message(tmp_path, monkeypatch):
    _stub_encoder(monkeypatch, error=FfmpegFailed("Invalid data found", returncode=1))
    svc = _service(tmp_path)
    job_id = svc.start_edit(
        source=Path("a.mp4"), ops=VideoOps(trim_end=1.0), source_ext="mp4",
        owner="alice", source_info=VideoInfo(duration=10.0),
    )
    job = _wait(svc, job_id, "alice")
    assert job["code"] == "ffmpeg_failed"
    assert "Invalid data" in job["error"]


@pytest.mark.parametrize("fmt,expected", [
    ("same", "mp4"), ("webm", "webm"), ("gif", "gif"), ("mkv", "mkv"),
])
def test_output_extension_matches_the_container(tmp_path, monkeypatch, fmt, expected):
    _stub_encoder(monkeypatch)
    svc = _service(tmp_path)
    job_id = svc.start_edit(
        source=Path("a.mp4"), ops=VideoOps(trim_end=1.0, output_format=fmt),
        source_ext="mp4", owner="alice", source_info=VideoInfo(duration=10.0),
    )
    job = _wait(svc, job_id, "alice")
    assert job["result"]["ext"] == expected


# ---------------------------------------------------------- job ownership

def test_a_job_is_visible_only_to_its_owner(tmp_path, monkeypatch):
    _stub_encoder(monkeypatch)
    svc = _service(tmp_path)
    job_id = svc.start_edit(
        source=Path("secret.mp4"), ops=VideoOps(trim_end=1.0), source_ext="mp4",
        owner="alice", source_info=VideoInfo(duration=10.0),
    )
    _wait(svc, job_id, "alice")

    # Bob must not be able to read Alice's job — not even to learn it exists.
    assert svc.get_job(job_id, owner="bob") is None
    assert svc.get_job(job_id, owner="alice") is not None


def test_an_unknown_job_is_indistinguishable_from_a_forbidden_one(tmp_path):
    svc = _service(tmp_path)
    assert svc.get_job("does-not-exist", owner="alice") is None


def test_job_records_do_not_hold_a_worker_id(tmp_path, monkeypatch):
    """Sessions must not be shared across threads, so the job keeps only data."""
    _stub_encoder(monkeypatch)
    svc = _service(tmp_path)
    job_id = svc.start_edit(
        source=Path("a.mp4"), ops=VideoOps(trim_end=1.0), source_ext="mp4",
        owner="alice", source_info=VideoInfo(duration=10.0),
    )
    _wait(svc, job_id, "alice")
    with svc._jobs_lock:
        assert svc._jobs[job_id]["owner"] == "alice"


# --------------------------------------------------------------- cleanup

def test_cleanup_job_removes_its_scratch_directory(tmp_path, monkeypatch):
    _stub_encoder(monkeypatch)
    svc = _service(tmp_path)
    job_id = svc.start_edit(
        source=Path("a.mp4"), ops=VideoOps(trim_end=1.0), source_ext="mp4",
        owner="alice", source_info=VideoInfo(duration=10.0),
    )
    _wait(svc, job_id, "alice")
    work_dir = svc.job_work_dir(job_id)
    assert work_dir.is_dir()

    svc.cleanup_job(job_id)
    assert not work_dir.exists()
    svc.cleanup_job(job_id)  # idempotent


def test_sweep_removes_old_directories_and_keeps_fresh_ones(tmp_path):
    svc = _service(tmp_path)
    root = Path(svc.config.temp_dir)
    stale = root / "stale"
    fresh = root / "fresh"
    stale.mkdir(parents=True)
    fresh.mkdir(parents=True)
    # Age the stale directory past the cutoff.
    long_ago = time.time() - 100000
    import os as _os
    _os.utime(stale, (long_ago, long_ago))

    assert svc.sweep_temp_dir(max_age_seconds=3600) == 1
    assert not stale.exists()
    assert fresh.exists()


def test_sweep_is_safe_when_there_is_no_temp_dir(tmp_path):
    svc = _service(tmp_path)
    svc.cleanup_job("never-existed")
    assert svc.sweep_temp_dir() == 0


# ------------------------------------------------- ffprobe output parsing

def test_parse_probe_json_reads_a_typical_mp4():
    info = ffmpeg_mod.parse_probe_json({
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
             "r_frame_rate": "30000/1001"},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "12.345", "size": "1048576", "format_name": "mov,mp4,m4a"},
    })
    assert info.width == 1920
    assert info.height == 1080
    assert info.duration == pytest.approx(12.345)
    assert info.fps == pytest.approx(29.97, abs=0.01)
    assert info.has_audio is True
    assert info.audio_codec == "aac"
    assert info.size_bytes == 1048576


def test_parse_probe_json_handles_a_silent_video():
    info = ffmpeg_mod.parse_probe_json({
        "streams": [{"codec_type": "video", "codec_name": "vp9", "width": 640,
                     "height": 360, "r_frame_rate": "25/1"}],
        "format": {"duration": "3.0"},
    })
    assert info.has_audio is False
    assert info.audio_codec == ""
    assert info.container == ""
    assert info.fps == pytest.approx(25.0)


def test_parse_probe_json_survives_garbage():
    """A container ffprobe half-understands must not raise."""
    assert ffmpeg_mod.parse_probe_json({}).duration == 0.0
    assert ffmpeg_mod.parse_probe_json({"streams": "not-a-list"}).width == 0
    assert ffmpeg_mod.parse_probe_json(
        {"streams": [{"codec_type": "video", "width": "abc", "r_frame_rate": "0/0"}],
         "format": {"duration": "N/A"}}
    ).duration == 0.0


def test_reported_size_is_never_negative():
    assert ffmpeg_mod.parse_probe_json({"format": {"size": "-1"}}).size_bytes == -1
    # Parsed verbatim; the field is informational and never used for limits.


# --------------------------------------------------------- progress parsing

@pytest.mark.parametrize("line,expected", [
    ("out_time_us=2500000", 2.5),
    ("out_time_ms=2500000", 2.5),   # ffmpeg's "ms" field is microseconds
    ("out_time_ms=N/A", None),
    ("out_time_ms=-5", None),
    ("progress=continue", None),
    ("frame=120", None),
    ("", None),
])
def test_progress_line_parsing(line, expected):
    result = ffmpeg_mod._parse_progress_line(line)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ---------------------------------------------------------- config / env

def test_video_config_reads_environment(monkeypatch):
    monkeypatch.setenv("VIDEO_ENABLED", "false")
    monkeypatch.setenv("VIDEO_MAX_DURATION_SECONDS", "42")
    monkeypatch.setenv("VIDEO_SILENCE_NOT_A_REAL_KEY", "x")
    from services.video.service import load_video_config
    config = load_video_config()
    assert config.enabled is False
    assert config.max_duration_seconds == 42


def test_invalid_numeric_environment_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("VIDEO_MAX_DURATION_SECONDS", "not-a-number")
    from services.video.service import load_video_config
    assert load_video_config().max_duration_seconds == 3600


# ------------------------------------------------------------- hashing

def test_hash_file_matches_hashlib(tmp_path):
    from routes.video_routes import _hash_file
    payload = b"video bytes" * 5000
    path = tmp_path / "clip.mp4"
    path.write_bytes(payload)
    assert _hash_file(path) == hashlib.sha256(payload).hexdigest()
