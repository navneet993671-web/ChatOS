"""Video editing REST routes.

Follows the gallery's ownership rules exactly, because an edit is just another
way to read and write gallery files:

* every route resolves the caller with ``get_current_user(request)``;
* a row that is missing **or** owned by someone else is a 404, so ids cannot be
  probed for another user's existence;
* a job is only visible to the user who created it;
* the local temp path of an encode is stripped before the job is returned —
  a filesystem path is not something the browser needs to know.

The heavy work runs on a worker thread via ``asyncio.to_thread`` (probes) or in
the service's own job thread (encodes), so a multi-minute re-encode never blocks
the event loop and chat stays responsive.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request

from core.database import GalleryImage, SessionLocal
from services.video import VideoEditError, get_video_service
from services.video.schemas import (
    VideoCapabilitiesOut,
    VideoEditRequest,
    VideoFrameRequest,
    VideoInfoOut,
    VideoJobOut,
)
from src.auth_helpers import get_current_user

logger = logging.getLogger(__name__)

# The files this module will treat as video. Kept in step with the upload
# route's VIDEO_EXTS and with the filename allow-list on
# /api/generated-image/{filename}.
VIDEO_EXTS = {"mp4", "mov", "webm", "mkv", "m4v"}

IMG_DIR = Path("data/generated_images")

# Errors from ffmpeg are user-facing, not bugs — map them onto honest status
# codes instead of a blanket 500.
_ERROR_STATUS = {
    "disabled": 503,
    "ffmpeg_unavailable": 503,
    "ffprobe_unavailable": 503,
    "too_long": 413,
    "too_large": 413,
    "probe_failed": 422,
    "frame_failed": 422,
    "invalid_trim": 400,
    "invalid_frame_time": 400,
    "empty_output": 500,
    "ffmpeg_failed": 500,
    "timeout": 504,
}


def _require_user(request: Request) -> str:
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user


def _safe_media_path(filename: str) -> Path:
    """Resolve a gallery filename inside the media directory.

    ``filename`` comes from the database and is normally a 12-hex name, but this
    still refuses anything that would escape the directory — a defence that
    costs nothing and removes a whole class of bug.
    """
    root = IMG_DIR.resolve()
    candidate = (IMG_DIR / filename).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(400, "Invalid media path")
    return candidate


def _owned_video_row(
    db, image_id: str, user: str
) -> Tuple[GalleryImage, Path, str]:
    """Fetch a gallery row the caller owns and prove it is a video."""
    row = db.query(GalleryImage).filter(GalleryImage.id == image_id).first()
    # Missing and not-yours are deliberately indistinguishable.
    if not row or not row.owner or row.owner != user:
        raise HTTPException(404, "Video not found")

    ext = row.filename.rsplit(".", 1)[-1].lower() if "." in row.filename else ""
    if ext not in VIDEO_EXTS:
        raise HTTPException(400, "That item is not a video")

    return row, _safe_media_path(row.filename), ext


def _hash_file(path: Path) -> str:
    """SHA-256 of a file without loading it into memory.

    A video can be gigabytes; the upload route can hash in memory because it
    already holds the bytes, but a file we just wrote cannot.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _video_error(exc: VideoEditError) -> HTTPException:
    return HTTPException(
        _ERROR_STATUS.get(getattr(exc, "code", ""), 500),
        {"error": str(exc), "code": getattr(exc, "code", "video_error"),
         "detail": getattr(exc, "detail", "")},
    )


def setup_video_routes(video_service=None) -> APIRouter:
    router = APIRouter(tags=["video"])
    service = video_service or get_video_service()

    # ---- GET /api/video/capabilities ----
    @router.get("/api/video/capabilities", response_model=VideoCapabilitiesOut)
    async def video_capabilities(request: Request, refresh: bool = False):
        """Report whether server-side editing is possible right now.

        ``refresh=true`` re-probes for the binaries, so installing ffmpeg while
        the server runs shows up on the next editor open instead of requiring a
        restart.
        """
        _require_user(request)
        return await asyncio.to_thread(service.capabilities, refresh=refresh)

    # ---- GET /api/video/{image_id}/info ----
    @router.get("/api/video/{image_id}/info", response_model=VideoInfoOut)
    async def video_info(request: Request, image_id: str):
        """Duration/dimensions/codecs, used to build the timeline."""
        user = _require_user(request)
        db = SessionLocal()
        try:
            _row, path, _ext = _owned_video_row(db, image_id, user)
        finally:
            db.close()

        if not path.exists():
            raise HTTPException(404, "Video file not found")

        try:
            info = await asyncio.to_thread(service.probe, path)
        except VideoEditError as exc:
            raise _video_error(exc)

        caps = service.capabilities()
        return {**info.as_dict(), "editable": bool(caps.get("server_editing"))}

    # ---- POST /api/video/{image_id}/edit ----
    @router.post("/api/video/{image_id}/edit")
    async def video_edit(request: Request, image_id: str, payload: VideoEditRequest):
        """Start an edit. Returns a job id to poll — never blocks the request."""
        user = _require_user(request)

        try:
            service.require_tools()
        except VideoEditError as exc:
            raise _video_error(exc)

        db = SessionLocal()
        try:
            row, path, ext = _owned_video_row(db, image_id, user)
            source_id = row.id
            source_prompt = row.prompt or ""
            source_album = row.album_id
        finally:
            db.close()

        if not path.exists():
            raise HTTPException(404, "Video file not found")

        try:
            info = await asyncio.to_thread(service.probe, path)
        except VideoEditError as exc:
            raise _video_error(exc)

        try:
            ops = payload.to_ops()
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        def _persist(result: Dict[str, Any]) -> Dict[str, Any]:
            """Move the finished encode into the gallery.

            Runs on the job thread with its own session, and is called *before*
            the job is marked done, so a job that reports success always has a
            gallery row behind it.
            """
            output = Path(result["output_path"])
            if not output.exists():
                raise VideoEditError(
                    "The encode finished but its output was missing",
                    code="empty_output",
                )

            new_filename = f"{uuid.uuid4().hex[:12]}.{result['ext']}"
            destination = IMG_DIR / new_filename
            IMG_DIR.mkdir(parents=True, exist_ok=True)

            to_save = payload.save_mode == "replace"
            # A replace always lands on a *new* filename. The media route serves
            # with `Cache-Control: immutable`, so overwriting bytes in place
            # would leave browsers showing the old video for a year.
            shutil.move(str(output), str(destination))

            try:
                file_hash = _hash_file(destination)
            except OSError as exc:
                destination.unlink(missing_ok=True)
                raise VideoEditError(f"Could not read the encoded file: {exc}", code="ffmpeg_failed")

            out_info = None
            try:
                out_info = service.probe(destination)
            except VideoEditError:
                out_info = None  # dimensions stay as-is; not worth failing over

            fresh = SessionLocal()
            try:
                if to_save:
                    target = fresh.query(GalleryImage).filter(GalleryImage.id == source_id).first()
                    if not target or target.owner != user:
                        destination.unlink(missing_ok=True)
                        raise VideoEditError("The original video is gone", code="empty_output")
                else:
                    # Same dedupe rule as the upload route, scoped to the user:
                    # a no-op edit should not litter the gallery with an
                    # identical second copy.
                    existing = (
                        fresh.query(GalleryImage)
                        .filter(
                            GalleryImage.file_hash == file_hash,
                            GalleryImage.is_active == True,  # noqa: E712
                            GalleryImage.owner == user,
                        )
                        .first()
                    )
                    if existing:
                        destination.unlink(missing_ok=True)
                        return {
                            "gallery_id": existing.id,
                            "filename": existing.filename,
                            "url": f"/api/generated-image/{existing.filename}",
                            "replaced": False,
                            "duplicate": True,
                        }
                    target = GalleryImage(
                        id=str(uuid.uuid4()),
                        filename=new_filename,
                        prompt=payload.name or (f"{source_prompt} (edited)" if source_prompt else "Edited video"),
                        model="video_edit",
                        owner=user,
                        album_id=source_album,
                        is_active=True,
                    )
                    fresh.add(target)

                old_filename = target.filename if to_save else None
                target.filename = new_filename
                target.file_hash = file_hash
                target.file_size = destination.stat().st_size
                if out_info and out_info.width and out_info.height:
                    target.width = out_info.width
                    target.height = out_info.height
                if payload.name:
                    target.prompt = payload.name

                fresh.commit()

                # Only remove the original once the new file is committed, so a
                # failed replace can never leave the user with neither.
                if old_filename and old_filename != new_filename:
                    _safe_media_path(old_filename).unlink(missing_ok=True)

                return {
                    "gallery_id": target.id,
                    "filename": target.filename,
                    "url": f"/api/generated-image/{target.filename}",
                    "replaced": to_save,
                    "width": target.width,
                    "height": target.height,
                }
            except HTTPException:
                raise
            except VideoEditError:
                fresh.rollback()
                raise
            except Exception as exc:
                fresh.rollback()
                destination.unlink(missing_ok=True)
                raise VideoEditError(f"Could not save the edited video: {exc}", code="ffmpeg_failed")
            finally:
                fresh.close()

        job_id = service.start_edit(
            source=path,
            ops=ops,
            source_ext=ext,
            owner=user,
            source_info=info,
            on_finished=_persist,
        )
        return {"job_id": job_id, "status": "queued"}

    # ---- GET /api/video/jobs/{job_id} ----
    @router.get("/api/video/jobs/{job_id}", response_model=VideoJobOut)
    async def video_job(request: Request, job_id: str):
        """Progress for the caller's own job. 404 for anyone else's."""
        user = _require_user(request)
        job = service.get_job(job_id, owner=user)
        if not job:
            raise HTTPException(404, "Job not found")

        status = job.get("status")
        if status in ("done", "error"):
            # Final poll: the output has already been moved into the gallery, so
            # the scratch directory is disposable. Keeps temp bounded without a
            # second request from the client.
            service.cleanup_job(job_id)

        # The local path of the encode is server-internal.
        if job.get("result"):
            job["result"] = {k: v for k, v in job["result"].items() if k != "output_path"}
        return job

    # ---- POST /api/video/{image_id}/frame ----
    @router.post("/api/video/{image_id}/frame")
    async def video_frame(request: Request, image_id: str, payload: VideoFrameRequest):
        """Save one frame as a gallery image.

        Extracted frames become ordinary gallery rows, which is what lets them
        open in the existing image editor and keep every album/tag/ownership
        behaviour instead of existing only in a video tool.
        """
        user = _require_user(request)

        try:
            service.require_tools()
        except VideoEditError as exc:
            raise _video_error(exc)

        db = SessionLocal()
        try:
            row, path, _ext = _owned_video_row(db, image_id, user)
            source_prompt = row.prompt or ""
            source_album = row.album_id
        finally:
            db.close()

        if not path.exists():
            raise HTTPException(404, "Video file not found")

        filename = f"{uuid.uuid4().hex[:12]}.png"
        destination = IMG_DIR / filename
        IMG_DIR.mkdir(parents=True, exist_ok=True)

        try:
            await asyncio.to_thread(
                service.extract_frame,
                source=path,
                at_seconds=payload.at,
                destination=destination,
                width=payload.width,
            )
        except VideoEditError as exc:
            raise _video_error(exc)

        try:
            content = destination.read_bytes()
        except OSError as exc:
            raise HTTPException(500, f"Could not read the extracted frame: {exc}")

        width = height = None
        try:
            from io import BytesIO

            from PIL import Image

            with Image.open(BytesIO(content)) as pil:
                width, height = pil.size
        except Exception:
            pass

        frame_id = str(uuid.uuid4())
        fresh = SessionLocal()
        try:
            fresh.add(
                GalleryImage(
                    id=frame_id,
                    filename=filename,
                    prompt=(f"{source_prompt} — frame @ {payload.at:.2f}s"
                            if source_prompt else f"Frame @ {payload.at:.2f}s"),
                    model="video_frame",
                    owner=user,
                    album_id=source_album,
                    is_active=True,
                    file_hash=hashlib.sha256(content).hexdigest(),
                    file_size=len(content),
                    width=width,
                    height=height,
                )
            )
            fresh.commit()
        except Exception as exc:
            fresh.rollback()
            destination.unlink(missing_ok=True)
            raise HTTPException(500, f"Could not save the extracted frame: {exc}")
        finally:
            fresh.close()

        return {
            "ok": True,
            "id": frame_id,
            "filename": filename,
            "url": f"/api/generated-image/{filename}",
            "width": width,
            "height": height,
        }

    return router
