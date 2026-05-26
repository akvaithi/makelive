"""Scan the user's Photos library for non-phone photo+video pairs that look
like candidates for Live Photo conversion.

Strategy:
    1. Fetch every PHAsset (image + video) sorted by `creationDate`.
    2. Walk them as a sorted timeline. For each video, find the closest
       unpaired photo within ±`window_seconds`. Closest by time wins.
    3. Skip:
         - photos that are already Live Photos (PHAssetMediaSubtypePhotoLive)
         - hidden / trashed assets (filtered via PHFetchOptions defaults)
    4. For each candidate pair, read the photo's EXIF Make/Model via
       PHImageManager and reject anything from a known phone manufacturer
       (Apple, Samsung, Google, etc.; Sony only if the model name looks like
       an Xperia).
    5. Survivors are returned as (photo_asset, video_asset, dt_seconds).

Reading EXIF only happens AFTER the cheap time-adjacency filter, so a 100k
asset library with 50 photo+video adjacencies costs ~50 EXIF reads — fast.

Exports use `PHAssetResourceManager.writeDataForAssetResource:toFile:` so
that the original PHAssets are never modified.
"""

from __future__ import annotations

import bisect
import os
import pathlib
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import objc
import Quartz
from Foundation import NSURL, NSDate, NSPredicate, NSSortDescriptor

from Photos import (
    PHAccessLevelReadWrite,
    PHAsset,
    PHAssetMediaSubtypePhotoLive,
    PHAssetMediaTypeImage,
    PHAssetMediaTypeVideo,
    PHAssetResource,
    PHAssetResourceManager,
    PHAssetResourceRequestOptions,
    PHAssetResourceTypePairedVideo,
    PHAssetResourceTypePhoto,
    PHAssetResourceTypeVideo,
    PHAuthorizationStatusAuthorized,
    PHAuthorizationStatusLimited,
    PHFetchOptions,
    PHImageManager,
    PHImageRequestOptions,
    PHImageRequestOptionsDeliveryModeFastFormat,
    PHPhotoLibrary,
)


# ---------- phone-vs-camera classifier ----------

# Known consumer-phone EXIF Make values (lowercased). Matching here disqualifies
# the photo. Sony is special-cased because they make both phones (Xperia) and
# dedicated cameras (Alpha) — see `_is_phone_photo`.
_PHONE_MAKES = {
    "apple",
    "samsung",
    "google",
    "oneplus",
    "xiaomi",
    "redmi",
    "huawei",
    "honor",
    "motorola",
    "lg",
    "lge",
    "asus",
    "oppo",
    "vivo",
    "realme",
    "nothing",
    "tcl",
    "alcatel",
    "blackberry",
    "lenovo",
    "zte",
    "essential",
}


def _is_phone_photo(make: str | None, model: str | None) -> bool:
    if not make:
        return False  # unknown — give benefit of the doubt
    m = make.strip().lower()
    if m in _PHONE_MAKES:
        return True
    if m == "sony":
        if model and ("xperia" in model.lower() or model.lower().startswith("xq-")):
            return True
        return False
    return False


# ---------- authorization ----------


class PhotosAccessError(RuntimeError):
    pass


def request_read_authorization() -> None:
    """Block until the user grants (or denies) read-write Photos access."""
    status = PHPhotoLibrary.authorizationStatusForAccessLevel_(PHAccessLevelReadWrite)
    if status in (PHAuthorizationStatusAuthorized, PHAuthorizationStatusLimited):
        return
    event = threading.Event()
    state: dict[str, int] = {}

    def handler(new_status):
        state["status"] = new_status
        event.set()

    PHPhotoLibrary.requestAuthorizationForAccessLevel_handler_(
        PHAccessLevelReadWrite, handler
    )
    event.wait()
    if state.get("status") not in (
        PHAuthorizationStatusAuthorized,
        PHAuthorizationStatusLimited,
    ):
        raise PhotosAccessError(
            "Photos library access was denied. Enable it in "
            "System Settings → Privacy & Security → Photos."
        )


# ---------- asset filename helpers ----------


def _asset_original_filename(asset) -> str:
    """Best-effort original filename from PHAssetResource (e.g. 'IMG_1234.HEIC')."""
    resources = PHAssetResource.assetResourcesForAsset_(asset)
    for r in resources:
        if r.type() in (
            PHAssetResourceTypePhoto,
            PHAssetResourceTypeVideo,
            PHAssetResourceTypePairedVideo,
        ):
            name = r.originalFilename()
            if name:
                return str(name)
    return f"asset_{asset.localIdentifier()}"


# ---------- candidate finder ----------


@dataclass
class Candidate:
    photo: object  # PHAsset (image)
    video: object  # PHAsset (video)
    dt_seconds: float
    photo_make: Optional[str] = None
    photo_model: Optional[str] = None

    @property
    def camera_key(self) -> tuple[Optional[str], Optional[str]]:
        return (self.photo_make, self.photo_model)

    @property
    def camera_label(self) -> str:
        if self.photo_make and self.photo_model:
            return f"{self.photo_make} {self.photo_model}"
        if self.photo_make:
            return self.photo_make
        if self.photo_model:
            return self.photo_model
        return "Unknown camera"


def _asset_time(asset) -> float:
    """Creation date as seconds since 1970, or +inf for missing dates."""
    d = asset.creationDate()
    if d is None:
        return float("inf")
    return d.timeIntervalSince1970()


def _is_live_photo_image(asset) -> bool:
    return bool(asset.mediaSubtypes() & PHAssetMediaSubtypePhotoLive)


def find_time_adjacent_candidates(
    window_seconds: float,
    *,
    date_from: Optional[NSDate] = None,
    date_to: Optional[NSDate] = None,
    max_video_duration: Optional[float] = 20.0,
) -> list[Candidate]:
    """Find every (photo, video) pair within `window_seconds`, closest-wins.

    Excludes assets already tagged as Live Photos. Does NOT filter by camera
    Make yet — that's done by `apply_phone_filter` so the caller can decide
    whether to spend the EXIF-read time.

    `date_from` / `date_to` (both inclusive) narrow the asset fetch via
    PHFetchOptions's predicate so we don't pull metadata we'll throw away.
    """
    request_read_authorization()

    # Fetch every image + video asset within the date range, sorted by creation date.
    options = PHFetchOptions.alloc().init()
    options.setSortDescriptors_(
        [NSSortDescriptor.sortDescriptorWithKey_ascending_("creationDate", True)]
    )
    media_predicate = NSPredicate.predicateWithFormat_(
        "mediaType == %d OR mediaType == %d",
        PHAssetMediaTypeImage,
        PHAssetMediaTypeVideo,
    )
    predicates = [media_predicate]
    if date_from is not None:
        predicates.append(NSPredicate.predicateWithFormat_("creationDate >= %@", date_from))
    if date_to is not None:
        predicates.append(NSPredicate.predicateWithFormat_("creationDate <= %@", date_to))
    if len(predicates) == 1:
        options.setPredicate_(predicates[0])
    else:
        from Foundation import NSCompoundPredicate
        options.setPredicate_(NSCompoundPredicate.andPredicateWithSubpredicates_(predicates))
    fetch_result = PHAsset.fetchAssetsWithOptions_(options)

    # Convert to a list — PHFetchResult is not directly iterable by index in PyObjC.
    n = fetch_result.count()
    all_assets = [fetch_result.objectAtIndex_(i) for i in range(n)]

    photos: list[tuple[float, object]] = []
    videos: list[tuple[float, object]] = []
    for a in all_assets:
        if a.mediaType() == PHAssetMediaTypeImage:
            if _is_live_photo_image(a):
                continue
            photos.append((_asset_time(a), a))
        elif a.mediaType() == PHAssetMediaTypeVideo:
            # Skip videos longer than the user's duration cap (None == no cap).
            if max_video_duration is not None and a.duration() > max_video_duration:
                continue
            videos.append((_asset_time(a), a))

    if not photos or not videos:
        return []

    photos.sort(key=lambda x: x[0])
    photo_times = [t for t, _ in photos]
    used = [False] * len(photos)

    candidates: list[Candidate] = []
    for vt, video in videos:
        lo = bisect.bisect_left(photo_times, vt - window_seconds)
        hi = bisect.bisect_right(photo_times, vt + window_seconds)
        best_i = -1
        best_dt = window_seconds + 1.0
        for i in range(lo, hi):
            if used[i]:
                continue
            dt = abs(photo_times[i] - vt)
            if dt < best_dt:
                best_dt = dt
                best_i = i
        if best_i >= 0:
            used[best_i] = True
            candidates.append(Candidate(photos[best_i][1], video, best_dt))

    return candidates


# ---------- EXIF Make/Model reader ----------


def _read_photo_make_model(asset) -> tuple[str | None, str | None]:
    """Pull EXIF Make + Model from a photo asset via PHImageManager.

    Uses delivery mode `FastFormat` and synchronous mode so the resultHandler
    runs before this function returns. Allows iCloud downloads; this can
    block briefly for assets not cached locally.
    """
    options = PHImageRequestOptions.alloc().init()
    options.setNetworkAccessAllowed_(True)
    options.setSynchronous_(True)
    options.setDeliveryMode_(PHImageRequestOptionsDeliveryModeFastFormat)

    result: dict = {}

    def handler(data, _data_uti, _orientation, _info):
        if data is None:
            return
        with objc.autorelease_pool():
            source = Quartz.CGImageSourceCreateWithData(data, None)
            if not source:
                return
            props = Quartz.CGImageSourceCopyPropertiesAtIndex(source, 0, None)
            if not props:
                return
            tiff = props.objectForKey_(Quartz.kCGImagePropertyTIFFDictionary)
            if tiff is None:
                return
            make = tiff.objectForKey_(Quartz.kCGImagePropertyTIFFMake)
            model = tiff.objectForKey_(Quartz.kCGImagePropertyTIFFModel)
            result["make"] = str(make) if make is not None else None
            result["model"] = str(model) if model is not None else None

    PHImageManager.defaultManager().requestImageDataAndOrientationForAsset_options_resultHandler_(
        asset, options, handler
    )
    return result.get("make"), result.get("model")


def apply_phone_filter(
    candidates: list[Candidate],
    *,
    exclude_phones: bool = True,
    progress_cb: Callable[[int, int], None] | None = None,
) -> list[Candidate]:
    """Read each candidate photo's EXIF Make/Model and (by default) drop
    pairs whose PHOTO was taken by a phone.

    Note: we only filter on the *photo's* maker — a video taken by a phone
    paired with a photo from a real camera is kept (that's the user's
    explicit intent).

    Each survivor is annotated with the resolved `photo_make` / `photo_model`
    so the GUI can group by camera.
    """
    surviving: list[Candidate] = []
    total = len(candidates)
    for i, cand in enumerate(candidates, start=1):
        make, model = _read_photo_make_model(cand.photo)
        cand.photo_make = make
        cand.photo_model = model
        if not exclude_phones or not _is_phone_photo(make, model):
            surviving.append(cand)
        if progress_cb is not None:
            progress_cb(i, total)
    return surviving


# ---------- export to disk ----------


def export_asset_to_file(asset, dest_dir: str | os.PathLike) -> pathlib.Path:
    """Write the asset's primary resource to a file inside `dest_dir`.

    Returns the path to the written file. Non-destructive: the original
    PHAsset is not modified.
    """
    dest_dir = pathlib.Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    resources = PHAssetResource.assetResourcesForAsset_(asset)
    # Prefer the "Photo" (or "Video") resource; if not found fall back to whatever's first.
    chosen = None
    preferred_types = (
        PHAssetResourceTypePhoto,
        PHAssetResourceTypeVideo,
        PHAssetResourceTypePairedVideo,
    )
    for pref in preferred_types:
        for r in resources:
            if r.type() == pref:
                chosen = r
                break
        if chosen is not None:
            break
    if chosen is None and resources:
        chosen = resources[0]
    if chosen is None:
        raise RuntimeError(f"No resources on asset {asset.localIdentifier()}")

    filename = str(chosen.originalFilename()) or f"asset_{asset.localIdentifier()}"
    dest_path = dest_dir / filename
    if dest_path.exists():
        dest_path.unlink()

    options = PHAssetResourceRequestOptions.alloc().init()
    options.setNetworkAccessAllowed_(True)

    event = threading.Event()
    state: dict = {}

    def completion(error):
        if error is not None:
            state["error"] = error.localizedDescription()
        event.set()

    PHAssetResourceManager.defaultManager().writeDataForAssetResource_toFile_options_completionHandler_(
        chosen,
        NSURL.fileURLWithPath_(str(dest_path)),
        options,
        completion,
    )
    event.wait()
    if "error" in state:
        raise RuntimeError(f"Photos export failed for {filename}: {state['error']}")
    return dest_path


# ---------- public entry point ----------


def scan_library(
    *,
    window_seconds: float = 300.0,
    exclude_phones: bool = True,
    date_from: Optional[NSDate] = None,
    date_to: Optional[NSDate] = None,
    max_video_duration: Optional[float] = 20.0,
    progress_cb: Callable[[str, int, int], None] | None = None,
) -> list[Candidate]:
    """Scan the Photos library for candidate Live-Photo conversions.

    Args:
        window_seconds: max |photo.creationDate − video.creationDate|.
        exclude_phones: drop pairs whose photo Make is a known phone maker
            (videos from phones are still allowed when the photo is from a
            non-phone camera).
        date_from / date_to: inclusive bounds on creationDate; either or both
            may be None to leave that side unconstrained.
        progress_cb: called as `progress_cb(stage, done, total)`. `stage` is
            "fetching" (asset enumeration) or "filtering" (EXIF read).

    Even when `exclude_phones=False`, this still reads EXIF for every
    candidate (so each result is annotated with photo_make / photo_model
    for grouping in the UI).
    """
    if progress_cb is not None:
        progress_cb("fetching", 0, 1)
    candidates = find_time_adjacent_candidates(
        window_seconds,
        date_from=date_from,
        date_to=date_to,
        max_video_duration=max_video_duration,
    )
    if progress_cb is not None:
        progress_cb("fetching", 1, 1)
    if progress_cb is not None:
        def _filter_progress(d, t):
            progress_cb("filtering", d, t)
        candidates = apply_phone_filter(
            candidates, exclude_phones=exclude_phones, progress_cb=_filter_progress
        )
    else:
        candidates = apply_phone_filter(candidates, exclude_phones=exclude_phones)
    return candidates
