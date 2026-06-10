"""Import a Live Photo pair into Apple Photos via PhotoKit."""

from __future__ import annotations

import pathlib
import threading

from Foundation import NSURL

from Photos import (
    PHAssetCreationRequest,
    PHAssetResourceCreationOptions,
    PHAssetResourceTypePairedVideo,
    PHAssetResourceTypePhoto,
    PHAuthorizationStatusAuthorized,
    PHPhotoLibrary,
)

try:
    from Photos import PHAccessLevelAddOnly, PHAccessLevelReadWrite
except ImportError:
    PHAccessLevelAddOnly = 1
    PHAccessLevelReadWrite = 2


class PhotosImportError(RuntimeError):
    """Raised when adding the Live Photo to the Photos library fails."""


def _request_authorization(level=None) -> None:
    """Block until we have at least the requested access level (default: add-only)."""
    if level is None:
        level = PHAccessLevelAddOnly
    status = PHPhotoLibrary.authorizationStatusForAccessLevel_(level)
    if status == PHAuthorizationStatusAuthorized:
        return

    event = threading.Event()
    result: dict[str, int] = {}

    def handler(new_status):
        result["status"] = new_status
        event.set()

    PHPhotoLibrary.requestAuthorizationForAccessLevel_handler_(level, handler)
    event.wait()
    if result.get("status") != PHAuthorizationStatusAuthorized:
        raise PhotosImportError(
            "Photos library access was not granted. "
            "Enable access in System Settings → Privacy & Security → Photos."
        )


def import_live_photo(photo_path: pathlib.Path, video_path: pathlib.Path) -> None:
    """Add a photo + paired video as a single Live Photo asset to the user's library."""
    _request_authorization()

    photo_url = NSURL.fileURLWithPath_(str(photo_path))
    video_url = NSURL.fileURLWithPath_(str(video_path))

    library = PHPhotoLibrary.sharedPhotoLibrary()
    state: dict = {}
    event = threading.Event()

    def changes_block():
        req = PHAssetCreationRequest.creationRequestForAsset()
        opts = PHAssetResourceCreationOptions.alloc().init()
        opts.setShouldMoveFile_(False)
        req.addResourceWithType_fileURL_options_(
            PHAssetResourceTypePhoto, photo_url, opts
        )
        req.addResourceWithType_fileURL_options_(
            PHAssetResourceTypePairedVideo, video_url, opts
        )

    def completion(success, error):
        if not success:
            if error is not None:
                desc = error.localizedDescription()
                code = error.code()
                domain = str(error.domain())
                # Failure reason often contains the actionable detail.
                reason = None
                try:
                    reason = error.localizedFailureReason()
                except Exception:
                    reason = None
                parts = [str(desc) if desc else "PhotoKit refused the import"]
                if reason:
                    parts.append(str(reason))
                parts.append(f"(code {code}, {domain})")
                state["error"] = " — ".join(parts)
            else:
                state["error"] = "unknown PhotoKit error"
        event.set()

    library.performChanges_completionHandler_(changes_block, completion)
    event.wait()
    if "error" in state:
        raise PhotosImportError(state["error"])
