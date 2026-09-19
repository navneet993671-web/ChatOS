"""Misantropic video editing — a capability layer over the existing gallery.

Video editing does not own storage. An edit produces a new file that the gallery
takes in through the same row shape as any other import, so albums, tags,
favourites, ownership and deletion all keep working unchanged. When ffmpeg is
absent the API says so and the browser encodes instead, rather than failing.
"""

from services.video.filters import (
    VIDEO_FORMATS,
    VideoOps,
    VideoOpsError,
    build_edit_args,
    build_frame_args,
    can_stream_copy,
    resolve_container,
)
from services.video.service import (
    VideoConfig,
    VideoEditError,
    VideoService,
    get_video_service,
    load_video_config,
    reset_video_service,
)

__all__ = [
    "VIDEO_FORMATS",
    "VideoConfig",
    "VideoEditError",
    "VideoOps",
    "VideoOpsError",
    "VideoService",
    "build_edit_args",
    "build_frame_args",
    "can_stream_copy",
    "get_video_service",
    "load_video_config",
    "reset_video_service",
    "resolve_container",
]
