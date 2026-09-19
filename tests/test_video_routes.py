"""Tests for routes/video_routes.py helpers and the video API schemas.

The route helpers carry the security properties — ownership, path containment,
and error mapping — so they are tested directly rather than only through an
HTTP client, which would need a live database for no extra coverage.
"""

from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from routes.video_routes import (
    VIDEO_EXTS,
    _owned_video_row,
    _safe_media_path,
    _video_error,
)
from services.video.schemas import (
    CropBox,
    VideoEditRequest,
    VideoFrameRequest,
)
from services.video.service import VideoEditError


class _FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._row


class _FakeDb:
    def __init__(self, row):
        self._row = row

    def query(self, *args, **kwargs):
        return _FakeQuery(self._row)


class _Row:
    def __init__(self, filename="abc123def456.mp4", owner="alice", prompt="clip", album_id=None):
        self.id = "row-1"
        self.filename = filename
        self.owner = owner
        self.prompt = prompt
        self.album_id = album_id


# ------------------------------------------------------------ path safety

def test_a_plain_gallery_filename_resolves_inside_the_media_dir():
    resolved = _safe_media_path("0123456789ab.mp4")
    assert resolved.name == "0123456789ab.mp4"
    assert resolved.parent.name == "generated_images"


@pytest.mark.parametrize("evil", [
    "../../../etc/passwd",
    "..\\..\\windows\\system32\\config",
    "subdir/../../escape.mp4",
    "../secret.mp4",
])
def test_traversal_filenames_are_refused(evil):
    """Filename comes from the DB, but containment is enforced anyway."""
    with pytest.raises(HTTPException) as excinfo:
        _safe_media_path(evil)
    assert excinfo.value.status_code == 400


def test_an_absolute_path_cannot_escape_the_media_dir():
    with pytest.raises(HTTPException):
        _safe_media_path("/etc/passwd")


# -------------------------------------------------------------- ownership

def test_a_video_owned_by_the_caller_is_returned():
    row, path, ext = _owned_video_row(_FakeDb(_Row()), "row-1", "alice")
    assert row.owner == "alice"
    assert ext == "mp4"
    assert path.name == "abc123def456.mp4"


def test_a_missing_row_is_a_404():
    with pytest.raises(HTTPException) as excinfo:
        _owned_video_row(_FakeDb(None), "row-1", "alice")
    assert excinfo.value.status_code == 404


def test_someone_elses_video_is_also_a_404():
    """Not a 403 — a 403 would confirm the id exists."""
    with pytest.raises(HTTPException) as excinfo:
        _owned_video_row(_FakeDb(_Row(owner="bob")), "row-1", "alice")
    assert excinfo.value.status_code == 404


def test_an_ownerless_row_is_not_claimable():
    with pytest.raises(HTTPException) as excinfo:
        _owned_video_row(_FakeDb(_Row(owner=None)), "row-1", "alice")
    assert excinfo.value.status_code == 404


@pytest.mark.parametrize("filename", ["abc.png", "abc.jpg", "abc.webp", "noext"])
def test_an_image_is_rejected_as_a_video(filename):
    with pytest.raises(HTTPException) as excinfo:
        _owned_video_row(_FakeDb(_Row(filename=filename)), "row-1", "alice")
    assert excinfo.value.status_code == 400


@pytest.mark.parametrize("ext", sorted(VIDEO_EXTS))
def test_every_supported_video_extension_is_accepted(ext):
    row, _path, parsed = _owned_video_row(
        _FakeDb(_Row(filename=f"abc123def456.{ext}")), "row-1", "alice"
    )
    assert parsed == ext


# ----------------------------------------------------------- error mapping

@pytest.mark.parametrize("code,status", [
    ("ffmpeg_unavailable", 503),
    ("disabled", 503),
    ("too_long", 413),
    ("too_large", 413),
    ("invalid_trim", 400),
    ("probe_failed", 422),
    ("ffmpeg_failed", 500),
    ("timeout", 504),
])
def test_video_errors_map_to_honest_status_codes(code, status):
    """Each failure needs a code the UI can branch on, not a blanket 500."""
    mapped = _video_error(VideoEditError("boom", code=code, detail="why"))
    assert mapped.status_code == status
    assert mapped.detail["code"] == code
    assert mapped.detail["error"] == "boom"


def test_an_unmapped_code_still_returns_a_structured_error():
    mapped = _video_error(VideoEditError("mystery", code="something_new"))
    assert mapped.status_code == 500
    assert mapped.detail["code"] == "something_new"


# ------------------------------------------------------------- validation

def test_rotate_must_be_a_quarter_turn():
    with pytest.raises(ValidationError):
        VideoEditRequest(rotate=45)


def test_rotate_wraps_past_a_full_turn():
    assert VideoEditRequest(rotate=450).to_ops().rotate == 90
    assert VideoEditRequest(rotate=-90).to_ops().rotate == 270


def test_trim_end_must_follow_trim_start():
    with pytest.raises(ValidationError):
        VideoEditRequest(trim_start=10.0, trim_end=5.0)
    # Equal is also a zero-length export, which is never what was meant.
    with pytest.raises(ValidationError):
        VideoEditRequest(trim_start=5.0, trim_end=5.0)


def test_trim_start_cannot_be_negative():
    with pytest.raises(ValidationError):
        VideoEditRequest(trim_start=-1.0)


@pytest.mark.parametrize("speed", [0.0, -1.0, 10.0])
def test_speed_is_bounded(speed):
    with pytest.raises(ValidationError):
        VideoEditRequest(speed=speed)


@pytest.mark.parametrize("volume", [-0.1, 9.0])
def test_volume_is_bounded(volume):
    with pytest.raises(ValidationError):
        VideoEditRequest(volume=volume)


def test_speed_and_volume_allow_their_edges():
    assert VideoEditRequest(speed=0.25).to_ops().speed == 0.25
    assert VideoEditRequest(speed=4.0).to_ops().speed == 4.0
    assert VideoEditRequest(volume=0.0).to_ops().mute is False  # silent, not removed


def test_unknown_output_format_is_refused():
    with pytest.raises(ValidationError):
        VideoEditRequest(output_format="avi")


def test_output_format_is_case_insensitive():
    assert VideoEditRequest(output_format="MP4").to_ops().output_format == "mp4"


@pytest.mark.parametrize("mode", ["new", "replace", "NEW"])
def test_known_save_modes(mode):
    assert VideoEditRequest(save_mode=mode).save_mode == mode.lower()


def test_an_unknown_save_mode_is_refused():
    with pytest.raises(ValidationError):
        VideoEditRequest(save_mode="overwrite-everything")


def test_save_mode_defaults_to_non_destructive():
    """The default must never overwrite the user's original."""
    assert VideoEditRequest().save_mode == "new"


def test_a_crop_that_leaves_the_frame_is_refused():
    with pytest.raises(ValidationError):
        VideoEditRequest(crop=CropBox(x=0.8, y=0.0, width=0.5, height=0.5))
    with pytest.raises(ValidationError):
        VideoEditRequest(crop=CropBox(x=0.0, y=0.8, width=0.5, height=0.5))


def test_a_crop_may_exactly_fill_the_frame():
    ops = VideoEditRequest(crop=CropBox(x=0.0, y=0.0, width=1.0, height=1.0)).to_ops()
    assert ops.crop == (0.0, 0.0, 1.0, 1.0)


def test_a_zero_sized_crop_is_refused():
    with pytest.raises(ValidationError):
        CropBox(x=0.0, y=0.0, width=0.0, height=0.5)


def test_a_name_is_trimmed_and_stripped_of_control_characters():
    ops = VideoEditRequest(name="  my clip\x00\x07  ").name
    assert ops == "my clip"


def test_a_name_of_only_whitespace_becomes_none():
    assert VideoEditRequest(name="   ").name is None


def test_a_name_is_length_capped():
    assert len(VideoEditRequest(name="x" * 5000).name) == 200


def test_to_ops_carries_every_field_through():
    ops = VideoEditRequest(
        trim_start=1.0, trim_end=2.0, rotate=180, flip_h=True, flip_v=True,
        crop=CropBox(x=0.1, y=0.1, width=0.5, height=0.5),
        mute=True, volume=0.5, speed=1.5, output_format="gif",
        gif_fps=15, gif_width=320,
    ).to_ops()
    assert ops.trim_start == 1.0
    assert ops.trim_end == 2.0
    assert ops.rotate == 180
    assert ops.flip_h and ops.flip_v
    assert ops.crop == (0.1, 0.1, 0.5, 0.5)
    assert ops.mute is True
    assert ops.volume == 0.5
    assert ops.speed == 1.5
    assert ops.output_format == "gif"
    assert ops.gif_fps == 15 and ops.gif_width == 320


def test_gif_quality_bounds():
    with pytest.raises(ValidationError):
        VideoEditRequest(gif_fps=0)
    with pytest.raises(ValidationError):
        VideoEditRequest(gif_width=99999)


def test_frame_request_bounds():
    assert VideoFrameRequest(at=0.0).at == 0.0
    with pytest.raises(ValidationError):
        VideoFrameRequest(at=-1.0)
    with pytest.raises(ValidationError):
        VideoFrameRequest(at=1.0, width=4)


# --------------------------------------------------- route module surface

def test_route_module_imports_without_ffmpeg_and_exposes_its_setup():
    """Importing the routes must not require the binary to be installed."""
    from routes.video_routes import setup_video_routes
    assert callable(setup_video_routes)


def test_media_dir_points_at_the_gallery_store():
    from routes.video_routes import IMG_DIR
    assert Path(IMG_DIR).name == "generated_images"


def test_uploadable_video_extensions_match_the_media_route_allowlist():
    """A mismatch here means an edit saves a file the browser cannot fetch."""
    from routes.video_routes import VIDEO_EXTS as route_exts

    media_route_allowlist = {"mp4", "mov", "webm", "mkv", "m4v"}
    assert route_exts == media_route_allowlist
    # And the gallery upload route accepts the same set.
    import re
    source = Path("routes/gallery_routes.py").read_text(encoding="utf-8")
    match = re.search(r"VIDEO_EXTS = \{([^}]*)\}", source)
    assert match, "upload route no longer declares VIDEO_EXTS"
    upload_exts = {part.strip().strip('"\'') for part in match.group(1).split(",") if part.strip()}
    assert upload_exts == route_exts
