"""Pydantic schemas for the video editing API.

The bounds here are the only place edit parameters are validated, so the
browser fallback and the server path cannot disagree about what is legal — the
frontend clamps to the same numbers, and anything a hand-crafted request
asserts is re-checked before it reaches an ffmpeg argv.

``save_mode`` defaults to ``"new"``: an edit produces a *new* gallery item and
leaves the original untouched. Replacing in place is an explicit opt-in, because
video re-encodes are lossy and a destructive default is a bad trade.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

from services.video.filters import VIDEO_FORMATS, VideoOps

# One place for every bound. Mirrored by static/js/videoEditor.js.
MIN_SPEED = 0.25
MAX_SPEED = 4.0
MIN_VOLUME = 0.0
MAX_VOLUME = 4.0
MAX_CROP_FRACTION = 1.0
SAVE_MODES = ("new", "replace")


class CropBox(BaseModel):
    """Crop rectangle as 0..1 fractions of the source frame.

    Fractions rather than pixels so the same request means the same thing
    whether the preview was downscaled or not.
    """

    x: float = Field(ge=0.0, le=MAX_CROP_FRACTION)
    y: float = Field(ge=0.0, le=MAX_CROP_FRACTION)
    width: float = Field(gt=0.0, le=MAX_CROP_FRACTION)
    height: float = Field(gt=0.0, le=MAX_CROP_FRACTION)

    @model_validator(mode="after")
    def _fits_inside_frame(self) -> "CropBox":
        if self.x + self.width > MAX_CROP_FRACTION + 1e-6:
            raise ValueError("crop extends past the right edge")
        if self.y + self.height > MAX_CROP_FRACTION + 1e-6:
            raise ValueError("crop extends past the bottom edge")
        return self

    def as_tuple(self) -> Tuple[float, float, float, float]:
        return (self.x, self.y, self.width, self.height)


class VideoEditRequest(BaseModel):
    """A non-destructive edit description."""

    trim_start: float = Field(default=0.0, ge=0.0)
    trim_end: Optional[float] = Field(default=None, gt=0.0)
    rotate: int = Field(default=0)
    flip_h: bool = False
    flip_v: bool = False
    crop: Optional[CropBox] = None
    mute: bool = False
    volume: float = Field(default=1.0, ge=MIN_VOLUME, le=MAX_VOLUME)
    speed: float = Field(default=1.0, ge=MIN_SPEED, le=MAX_SPEED)
    output_format: str = Field(default="same")
    gif_fps: int = Field(default=12, ge=1, le=30)
    gif_width: int = Field(default=480, ge=64, le=1920)

    save_mode: str = Field(default="new")
    name: Optional[str] = None

    @field_validator("rotate")
    @classmethod
    def _quarter_turns_only(cls, value: int) -> int:
        if value % 90 != 0:
            raise ValueError("rotate must be a multiple of 90")
        return value % 360

    @field_validator("output_format")
    @classmethod
    def _known_format(cls, value: str) -> str:
        normalised = (value or "same").lower().lstrip(".")
        if normalised not in VIDEO_FORMATS:
            raise ValueError(f"output_format must be one of {', '.join(VIDEO_FORMATS)}")
        return normalised

    @field_validator("save_mode")
    @classmethod
    def _known_save_mode(cls, value: str) -> str:
        normalised = (value or "new").lower()
        if normalised not in SAVE_MODES:
            raise ValueError(f"save_mode must be one of {', '.join(SAVE_MODES)}")
        return normalised

    @field_validator("name")
    @classmethod
    def _sane_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()[:200]
        # Control characters only cause trouble downstream (and in logs).
        return "".join(ch for ch in cleaned if ch.isprintable()) or None

    @model_validator(mode="after")
    def _trim_range_is_valid(self) -> "VideoEditRequest":
        if self.trim_end is not None and self.trim_end <= self.trim_start:
            raise ValueError("trim_end must be greater than trim_start")
        return self

    def to_ops(self) -> VideoOps:
        return VideoOps(
            trim_start=float(self.trim_start),
            trim_end=None if self.trim_end is None else float(self.trim_end),
            rotate=int(self.rotate),
            flip_h=bool(self.flip_h),
            flip_v=bool(self.flip_v),
            crop=self.crop.as_tuple() if self.crop else None,
            mute=bool(self.mute),
            volume=float(self.volume),
            speed=float(self.speed),
            output_format=self.output_format,
            gif_fps=int(self.gif_fps),
            gif_width=int(self.gif_width),
        )


class VideoFrameRequest(BaseModel):
    """Save the frame at ``at`` seconds as a still."""

    at: float = Field(ge=0.0)
    width: Optional[int] = Field(default=None, ge=16, le=7680)


class VideoCapabilitiesOut(BaseModel):
    """What this server can actually do right now.

    ``ffmpeg`` false is not an error state — it tells the browser to use its own
    encoder. ``ffprobe`` can be absent on its own, in which case edits still work
    but metadata probing does not.
    """

    ffmpeg: bool
    ffprobe: bool
    version: str = ""
    install_hint: str = ""
    formats: List[str]
    server_editing: bool
    max_duration_seconds: int
    max_output_bytes: int


class VideoInfoOut(BaseModel):
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    video_codec: str = ""
    audio_codec: str = ""
    container: str = ""
    size_bytes: int = 0
    editable: bool = True


class VideoJobOut(BaseModel):
    """Progress envelope for a running or finished edit.

    ``progress`` is 0..1 from ffmpeg's own output; ``status`` is one of
    ``queued``, ``running``, ``done``, ``error``.
    """

    job_id: str
    status: str
    progress: float = 0.0
    message: str = ""
    error: Optional[str] = None
    code: Optional[str] = None
    result: Optional[dict] = None


class VideoErrorOut(BaseModel):
    """Typed failure so the UI renders a specific message, never a raw 500."""

    error: str
    code: str
    detail: Optional[str] = None
