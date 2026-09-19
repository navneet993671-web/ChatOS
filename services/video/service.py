"""Video editing service.

Boundaries this module holds:

* **Edits are non-destructive by default.** An encode writes a *new* file and
  never touches the source. Overwriting is the caller's decision, made after the
  new file already exists on disk.
* **No shell.** Every command is an argv list built by
  ``services.video.filters`` and executed by ``services.video.ffmpeg``.
* **Bounded.** Input duration, output size and wall-clock time are all capped,
  and every job's scratch directory is deleted when it finishes or is swept.
* **Jobs belong to a user.** A job records its owner, and lookups are scoped to
  that owner, so one user cannot poll another's job id and read their filename
  or error text.
* **Missing ffmpeg is not an error.** It is a reported capability; the browser
  encodes instead.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from services.video import ffmpeg as ffmpeg_mod
from services.video.filters import (
    VideoOps,
    build_edit_args,
    build_frame_args,
    can_stream_copy,
    resolve_container,
)
from services.video.ffmpeg import (
    FfmpegFailed,
    FfmpegTimeout,
    FfmpegTools,
    FfmpegUnavailable,
    VideoInfo,
)

logger = logging.getLogger(__name__)


class VideoEditError(RuntimeError):
    """An edit that cannot proceed, with a machine-readable ``code``.

    The code matters: "too_long" needs a different UI affordance from
    "ffmpeg_failed", and neither should surface as an opaque 500.
    """

    def __init__(self, message: str, *, code: str = "video_error", detail: str = ""):
        super().__init__(message)
        self.code = code
        self.detail = detail


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default


@dataclass(frozen=True)
class VideoConfig:
    """Video configuration, from ``VIDEO_*`` environment variables."""

    enabled: bool = True
    max_duration_seconds: int = 3600
    max_output_bytes: int = 2 * 1024 * 1024 * 1024
    timeout_seconds: int = 1800
    temp_dir: str = "data/tmp/video"
    keep_failed_outputs: bool = False


def load_video_config() -> VideoConfig:
    return VideoConfig(
        enabled=_env_bool("VIDEO_ENABLED", True),
        max_duration_seconds=_env_int("VIDEO_MAX_DURATION_SECONDS", 3600),
        max_output_bytes=_env_int("VIDEO_MAX_OUTPUT_BYTES", 2 * 1024 * 1024 * 1024),
        timeout_seconds=_env_int("VIDEO_FFMPEG_TIMEOUT_SECONDS", 1800),
        temp_dir=os.environ.get("VIDEO_TEMP_DIR", "data/tmp/video"),
        keep_failed_outputs=_env_bool("VIDEO_KEEP_FAILED_OUTPUTS", False),
    )


# How many finished jobs to remember, and for how long. Jobs are progress
# envelopes, not history — the gallery row is the durable record — so these are
# deliberately small.
_MAX_JOBS = 24
_JOB_TTL_SECONDS = 3600


class VideoService:
    def __init__(
        self,
        config: Optional[VideoConfig] = None,
        *,
        tools_factory: Callable[..., Optional[FfmpegTools]] = ffmpeg_mod.detect_tools,
    ):
        self.config = config or load_video_config()
        self._tools_factory = tools_factory
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._jobs_lock = threading.Lock()

    # ------------------------------------------------------------------ tools

    def tools(self, *, refresh: bool = False) -> Optional[FfmpegTools]:
        return self._tools_factory(refresh=refresh)

    def capabilities(self, *, refresh: bool = False) -> Dict[str, Any]:
        """Report what this server can do, without raising when it cannot."""
        tools = self.tools(refresh=refresh)
        return {
            "ffmpeg": bool(tools and tools.ffmpeg),
            "ffprobe": bool(tools and tools.ffprobe),
            "version": tools.version if tools else "",
            "install_hint": "" if tools else ffmpeg_mod.install_hint(),
            "formats": list(("same", "mp4", "webm", "gif", "mkv")),
            "server_editing": bool(self.config.enabled and tools and tools.ffmpeg),
            "max_duration_seconds": self.config.max_duration_seconds,
            "max_output_bytes": self.config.max_output_bytes,
        }

    def require_tools(self) -> FfmpegTools:
        if not self.config.enabled:
            raise VideoEditError(
                "Video editing is disabled on this server",
                code="disabled",
                detail="Set VIDEO_ENABLED=true to turn it on.",
            )
        tools = self.tools()
        if not tools or not tools.ffmpeg:
            raise VideoEditError(
                "ffmpeg is not installed on this server",
                code="ffmpeg_unavailable",
                detail=ffmpeg_mod.install_hint(),
            )
        return tools

    def probe(self, path: Path) -> VideoInfo:
        """Metadata for ``path``. Returns an empty ``VideoInfo`` if ffprobe is
        unavailable rather than failing — callers treat 0 duration as unknown."""
        try:
            return ffmpeg_mod.probe(path, self.tools())
        except FfmpegUnavailable:
            return VideoInfo()
        except FfmpegFailed as exc:
            raise VideoEditError(
                "Could not read this video file",
                code="probe_failed",
                detail=str(exc),
            )

    # ------------------------------------------------------------------- jobs

    def _prune_jobs_locked(self) -> None:
        """Drop finished jobs past their TTL, then the oldest if still over cap.

        Called with ``_jobs_lock`` held.
        """
        now = time.time()
        for job_id in [
            jid for jid, job in self._jobs.items()
            if job["status"] in ("done", "error")
            and now - job.get("finished_at", now) > _JOB_TTL_SECONDS
        ]:
            self._jobs.pop(job_id, None)

        overflow = len(self._jobs) - _MAX_JOBS
        if overflow > 0:
            finished = sorted(
                (jid for jid, job in self._jobs.items() if job["status"] in ("done", "error")),
                key=lambda jid: self._jobs[jid].get("finished_at", 0),
            )
            for jid in finished[:overflow]:
                self._jobs.pop(jid, None)

    def _job_view(self, job: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "progress": round(float(job.get("progress", 0.0)), 4),
            "message": job.get("message", ""),
            "error": job.get("error"),
            "code": job.get("code"),
            "result": job.get("result"),
        }

    def get_job(self, job_id: str, *, owner: str) -> Optional[Dict[str, Any]]:
        """A job, only if it belongs to ``owner``.

        Returns ``None`` for both "does not exist" and "belongs to someone
        else", so a job id cannot be probed for another user's existence.
        """
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job or job.get("owner") != owner:
                return None
            return self._job_view(job)

    def job_work_dir(self, job_id: str) -> Path:
        return Path(self.config.temp_dir) / job_id

    def cleanup_job(self, job_id: str) -> None:
        """Remove a job's scratch directory. Safe to call twice."""
        shutil.rmtree(self.job_work_dir(job_id), ignore_errors=True)

    def sweep_temp_dir(self, *, max_age_seconds: int = 86400) -> int:
        """Delete scratch directories older than ``max_age_seconds``.

        A crash mid-encode leaves a directory behind; without this the temp
        directory grows without bound. Returns how many were removed.
        """
        root = Path(self.config.temp_dir)
        if not root.is_dir():
            return 0
        cutoff = time.time() - max_age_seconds
        removed = 0
        for child in root.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        return removed

    def start_edit(
        self,
        *,
        source: Path,
        ops: VideoOps,
        source_ext: str,
        owner: str,
        source_info: Optional[VideoInfo] = None,
        on_finished: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> str:
        """Kick off an encode in a background thread; return the job id.

        Returned immediately so the HTTP request is short and the browser can
        poll for genuine progress. ffmpeg reports progress on its own schedule
        and a long re-encode is normal, so a held request would be fragile
        (proxies time out, tabs suspend).

        ``on_finished`` is called with the completed result *before* the job is
        marked done, and whatever dict it returns is merged into that result.
        That is how the caller persists the output into the gallery, so the
        client never observes "done" for a file that has not been saved yet. An
        exception from it fails the job rather than leaving a dangling file.
        """
        self.require_tools()
        self.validate_ops_against_source(ops, source_info)

        job_id = uuid.uuid4().hex[:16]
        work_dir = self.job_work_dir(job_id)
        work_dir.mkdir(parents=True, exist_ok=True)

        with self._jobs_lock:
            self._prune_jobs_locked()
            self._jobs[job_id] = {
                "job_id": job_id,
                "owner": owner,
                "status": "queued",
                "progress": 0.0,
                "message": "Starting…",
                "error": None,
                "code": None,
                "result": None,
                "created_at": time.time(),
            }

        thread = threading.Thread(
            target=self._run_edit,
            kwargs=dict(
                job_id=job_id,
                source=source,
                ops=ops,
                source_ext=source_ext,
                source_info=source_info,
                work_dir=work_dir,
                on_finished=on_finished,
            ),
            name=f"video-edit-{job_id}",
            daemon=True,
        )
        thread.start()
        return job_id

    def validate_ops_against_source(
        self, ops: VideoOps, info: Optional[VideoInfo]
    ) -> None:
        """Reject an edit that cannot be honoured, before spawning ffmpeg."""
        if not info or info.duration <= 0:
            return  # unknown duration: ffmpeg will just stop at EOF
        if ops.trim_start >= info.duration:
            raise VideoEditError(
                "The trim start is past the end of the video",
                code="invalid_trim",
                detail=f"Video is {info.duration:.1f}s long.",
            )
        kept = ops.trim_duration
        effective = info.duration - ops.trim_start if kept is None else kept
        if effective > self.config.max_duration_seconds:
            raise VideoEditError(
                "This clip is longer than the server's limit",
                code="too_long",
                detail=(
                    f"{effective:.0f}s selected, limit is "
                    f"{self.config.max_duration_seconds}s. Trim it first, or raise "
                    f"VIDEO_MAX_DURATION_SECONDS."
                ),
            )

    def _set_job(self, job_id: str, **fields: Any) -> None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job:
                job.update(fields)

    def _run_edit(
        self,
        *,
        job_id: str,
        source: Path,
        ops: VideoOps,
        source_ext: str,
        source_info: Optional[VideoInfo],
        work_dir: Path,
        on_finished: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> None:
        started = time.time()
        output_path = work_dir / f"output.{self._output_ext(ops, source_ext)}"
        try:
            tools = self.tools()
            if not tools or not tools.ffmpeg:
                raise VideoEditError("ffmpeg became unavailable", code="ffmpeg_unavailable")

            has_audio = bool(source_info and source_info.has_audio)
            stream_copy = can_stream_copy(ops, source_ext)

            self._set_job(
                job_id,
                status="running",
                message="Trimming without re-encoding…" if stream_copy else "Encoding…",
            )

            args = build_edit_args(
                ffmpeg=tools.ffmpeg,
                source=str(source),
                destination=str(output_path),
                ops=ops,
                source_ext=source_ext,
                source_has_audio=has_audio,
                stream_copy=stream_copy,
            )
            logger.info("video edit %s: %s", job_id, " ".join(args))

            total = self._expected_duration(ops, source_info, stream_copy)

            ffmpeg_mod.run_ffmpeg_streaming(
                args,
                timeout=float(self.config.timeout_seconds),
                total_seconds=total,
                on_progress=lambda fraction: self._set_job(job_id, progress=fraction),
            )

            if not output_path.exists() or output_path.stat().st_size == 0:
                raise VideoEditError(
                    "The encode produced no output", code="empty_output"
                )

            size = output_path.stat().st_size
            if size > self.config.max_output_bytes:
                output_path.unlink(missing_ok=True)
                raise VideoEditError(
                    "The edited video is larger than the server's limit",
                    code="too_large",
                    detail=(
                        f"{size / 1_048_576:.0f} MB produced, limit is "
                        f"{self.config.max_output_bytes / 1_048_576:.0f} MB. "
                        f"Try a lower resolution or a shorter trim."
                    ),
                )

            result: Dict[str, Any] = {
                "output_path": str(output_path),
                "container": resolve_container(source_ext, ops.output_format),
                "ext": output_path.suffix.lstrip("."),
                "size_bytes": size,
                "elapsed_seconds": round(time.time() - started, 2),
                "stream_copy": stream_copy,
            }
            if on_finished is not None:
                result.update(on_finished(result) or {})

            self._set_job(
                job_id,
                status="done",
                progress=1.0,
                message="Finished",
                finished_at=time.time(),
                result=result,
            )
        except (VideoEditError, FfmpegFailed, OSError) as exc:
            code = getattr(exc, "code", None)
            if code is None:
                code = "timeout" if isinstance(exc, FfmpegTimeout) else "ffmpeg_failed"
            logger.warning("video edit %s failed (%s): %s", job_id, code, exc)
            # A failed job keeps no output; the scratch dir is swept either by
            # cleanup_job or the TTL sweeper.
            if not self.config.keep_failed_outputs:
                output_path.unlink(missing_ok=True)
            self._set_job(
                job_id,
                status="error",
                error=str(exc) or "Video edit failed",
                code=code,
                message="Failed",
                finished_at=time.time(),
            )

    def _output_ext(self, ops: VideoOps, source_ext: str) -> str:
        container = resolve_container(source_ext, ops.output_format)
        # Everything mp4-family is written as .mp4 so players pick the right
        # demuxer; mkv and webm keep their own names.
        return {"mp4": "mp4", "webm": "webm", "mkv": "mkv", "gif": "gif"}.get(
            container, "mp4"
        )

    def _expected_duration(
        self, ops: VideoOps, info: Optional[VideoInfo], stream_copy: bool
    ) -> Optional[float]:
        """What the output should last, for progress maths.

        Speed changes the timeline length, which is why it is divided out here;
        otherwise the progress bar would stall at 50% on a 2x export.
        """
        if not info or info.duration <= 0:
            return None
        kept = ops.trim_duration
        length = info.duration - ops.trim_start if kept is None else kept
        if length <= 0:
            return None
        if stream_copy:
            return length
        return max(0.001, length / ops.speed)

    # ------------------------------------------------------------------ frame

    def extract_frame(
        self,
        *,
        source: Path,
        at_seconds: float,
        destination: Path,
        width: Optional[int] = None,
    ) -> Path:
        """Write the frame at ``at_seconds`` as a PNG to ``destination``."""
        tools = self.require_tools()
        if at_seconds < 0:
            raise VideoEditError("Frame time cannot be negative", code="invalid_frame_time")

        destination.parent.mkdir(parents=True, exist_ok=True)
        args = build_frame_args(
            ffmpeg=tools.ffmpeg,
            source=str(source),
            destination=str(destination),
            at_seconds=at_seconds,
            width=width,
        )
        try:
            ffmpeg_mod.run_ffmpeg(args, timeout=120)
        except FfmpegFailed as exc:
            raise VideoEditError(
                "Could not extract that frame",
                code="frame_failed",
                detail=str(exc),
            )
        if not destination.exists() or destination.stat().st_size == 0:
            raise VideoEditError(
                "Could not extract that frame — is it past the end of the video?",
                code="frame_failed",
            )
        return destination


_service: Optional[VideoService] = None
_service_lock = threading.Lock()


def get_video_service() -> VideoService:
    global _service
    with _service_lock:
        if _service is None:
            _service = VideoService()
            # Opportunistic cleanup of scratch dirs orphaned by a previous run.
            try:
                removed = _service.sweep_temp_dir()
                if removed:
                    logger.info("Swept %d stale video temp dir(s)", removed)
            except Exception:
                logger.debug("video temp sweep failed", exc_info=True)
        return _service


def reset_video_service() -> None:
    """Drop the singleton (used by tests)."""
    global _service
    with _service_lock:
        _service = None
    ffmpeg_mod.reset_tools_cache()
