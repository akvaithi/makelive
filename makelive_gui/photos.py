"""Import a Live Photo pair into Apple Photos via PhotoKit."""

from __future__ import annotations

import pathlib
import threading

from Foundation import NSURL, NSPredicate

from Photos import (
    PHAssetCollection,
    PHAssetCollectionChangeRequest,
    PHAssetCollectionSubtypeAlbumRegular,
    PHAssetCollectionTypeAlbum,
    PHAssetCreationRequest,
    PHAssetResourceCreationOptions,
    PHAssetResourceTypePairedVideo,
    PHAssetResourceTypePhoto,
    PHAuthorizationStatusAuthorized,
    PHFetchOptions,
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


def _find_album(album_name: str):
    """Return an existing user album with the given title, or None."""
    options = PHFetchOptions.alloc().init()
    options.setPredicate_(
        NSPredicate.predicateWithFormat_("title == %@", album_name)
    )
    fetch = PHAssetCollection.fetchAssetCollectionsWithType_subtype_options_(
        PHAssetCollectionTypeAlbum,
        PHAssetCollectionSubtypeAlbumRegular,
        options,
    )
    if fetch.count() > 0:
        return fetch.objectAtIndex_(0)
    return None


def _create_album(album_name: str):
    """Create a new user album with the given title and return its local
    identifier (so we can re-fetch it in the next changes block).

    This is a separate `performChanges` call from the asset-import one
    because PHObjectPlaceholders from a creation request are only valid
    *within* the same changes block; we want a real PHAssetCollection
    handle for the import block, so we materialise it first.
    """
    library = PHPhotoLibrary.sharedPhotoLibrary()
    state: dict = {}
    placeholder_id: dict = {}
    event = threading.Event()

    def changes_block():
        req = PHAssetCollectionChangeRequest.creationRequestForAssetCollectionWithTitle_(
            album_name
        )
        placeholder = req.placeholderForCreatedAssetCollection()
        placeholder_id["id"] = str(placeholder.localIdentifier())

    def completion(success, error):
        if not success:
            state["error"] = (
                error.localizedDescription() if error else "unknown error"
            )
        event.set()

    library.performChanges_completionHandler_(changes_block, completion)
    event.wait()
    if "error" in state:
        raise PhotosImportError(
            f"Could not create album '{album_name}': {state['error']}"
        )
    return placeholder_id.get("id")


def _ensure_album(album_name: str):
    """Find the user album with `album_name`, creating it if absent.
    Returns the PHAssetCollection (re-fetched after creation if needed)."""
    album = _find_album(album_name)
    if album is not None:
        return album
    local_id = _create_album(album_name)
    if not local_id:
        return None
    # Re-fetch by localIdentifier so we have a stable PHAssetCollection handle.
    fetch = PHAssetCollection.fetchAssetCollectionsWithLocalIdentifiers_options_(
        [local_id], None
    )
    if fetch.count() > 0:
        return fetch.objectAtIndex_(0)
    return None


def import_live_photo(
    photo_path: pathlib.Path,
    video_path: pathlib.Path,
    *,
    album_name: str | None = "makelive",
) -> None:
    """Add a photo + paired video as a single Live Photo asset to Photos.

    If `album_name` is given (defaults to "makelive"), the new asset is
    also added to a user album with that name — created if it doesn't
    exist yet. Pass `album_name=None` to skip album organisation.
    """
    # Creating an album requires read+write access; add-only is not enough.
    needs_album = album_name is not None
    _request_authorization(
        PHAccessLevelReadWrite if needs_album else None
    )

    album = _ensure_album(album_name) if needs_album else None

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
        # Add the new asset to the album in the same transaction so the
        # write is atomic — either both succeed or both roll back.
        if album is not None:
            placeholder = req.placeholderForCreatedAsset()
            album_request = (
                PHAssetCollectionChangeRequest.changeRequestForAssetCollection_(album)
            )
            if album_request is not None and placeholder is not None:
                album_request.addAssets_([placeholder])

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
