"""Main window: batch Live-Photo creator with a Liquid-Glass-ish appearance."""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import threading
import traceback

import objc
import Quartz
import AVFoundation
import CoreMedia
from Foundation import NSLog
from AppKit import (
    NSApp,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSButtonTypeSwitch,
    NSControlStateValueOn,
    NSColor,
    NSDragOperationCopy,
    NSDragOperationNone,
    NSFont,
    NSImageScaleProportionallyUpOrDown,
    NSImageView,
    NSMenu,
    NSMenuItem,
    NSOpenPanel,
    NSPasteboardTypeFileURL,
    NSScrollView,
    NSTableColumn,
    NSTableView,
    NSTableViewSelectionHighlightStyleNone,
    NSTableViewStyleInset,
    NSTextAlignmentCenter,
    NSTextAlignmentLeft,
    NSTextAlignmentRight,
    NSTextField,
    NSView,
    NSViewHeightSizable,
    NSViewMaxXMargin,
    NSViewMaxYMargin,
    NSViewMinXMargin,
    NSViewMinYMargin,
    NSViewWidthSizable,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectBlendingModeWithinWindow,
    NSVisualEffectMaterialContentBackground,
    NSVisualEffectMaterialUnderWindowBackground,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSWindowTitleHidden,
    NSWorkspace,
)
from AppKit import NSImage  # noqa: E402  -- used in _symbol
from Foundation import NSMakeRect, NSObject, NSOperationQueue, NSURL

from makelive.__main__ import find_photo_video_pairs
from makelive.makelive import is_image_file, is_video_file, make_live_photo
from makelive.video_reencode import reencode_to_hevc

from .photos import PhotosImportError, import_live_photo


# ---------- constants / layout ----------

WIN_W, WIN_H = 780, 740
TITLEBAR_INSET = 28              # height we reserve for traffic-light buttons
HEADER_H = 78
TOOLBAR_H = 52
COLUMNS_H = 220                  # two-column unpaired section (photos | videos)
PAIR_BAR_H = 44                  # row with the "Pair Selected" button
FOOTER_H = 92
ROW_HEIGHT = 56
SIMPLE_ROW_HEIGHT = 32
EDGE_PAD = 16
COL_GAP = 12

DEFAULT_HEVC_BITRATE = 10_000_000  # 10 Mbps

STATUS_COLOR = {
    "pending":    ("clock", "secondaryLabelColor"),
    "processing": ("arrow.triangle.2.circlepath", "systemBlueColor"),
    "added":      ("checkmark.circle.fill", "systemGreenColor"),
    "failed":     ("exclamationmark.triangle.fill", "systemRedColor"),
}

STATUS_TEXT = {
    "pending":    "Pending",
    "processing": "Processing…",
    "added":      "Added",
    "failed":     "Failed",
}


# ---------- helpers ----------


def _label(text: str, *, size=12, bold=False, color=None, align=NSTextAlignmentLeft):
    f = NSTextField.alloc().init()
    f.setStringValue_(text)
    f.setBezeled_(False)
    f.setDrawsBackground_(False)
    f.setEditable_(False)
    f.setSelectable_(False)
    f.setAlignment_(align)
    f.setFont_(
        NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
    )
    f.setTextColor_(color or NSColor.labelColor())
    f.setLineBreakMode_(4)  # NSLineBreakByTruncatingMiddle
    return f


def _color(name: str) -> NSColor:
    """Resolve a named NSColor at draw time so dark/light adapts live."""
    return getattr(NSColor, name)()


def _symbol(name: str, *, point_size=15, weight=None) -> NSImage | None:
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    return img


# ---------- safe main-queue dispatch ----------


def _post_to_main(fn):
    """Dispatch `fn` onto the main NSOperationQueue with a try/except wrapper.

    A Python exception escaping a main-queue block is converted by PyObjC into
    an Obj-C exception, which AppKit doesn't catch — the process aborts with a
    bare backtrace and no Python frame info. Wrapping every block here means
    any failure is logged to Console.app and /tmp/makelive_crash.log instead
    of taking the app down.
    """
    def safe():
        try:
            fn()
        except Exception as exc:
            tb = traceback.format_exc()
            try:
                NSLog("[makelive] main-queue block crash: %@", f"{exc}\n{tb}")
            except Exception:
                pass
            try:
                with open("/tmp/makelive_crash.log", "a") as fp:
                    fp.write(f"--- main-queue block crash ---\n{exc}\n{tb}\n")
            except Exception:
                pass

    NSOperationQueue.mainQueue().addOperationWithBlock_(safe)


# ---------- thumbnail helpers ----------

# Cache keyed by "file:<path>" or "asset:<localIdentifier>". Bounded loosely
# via a max-size sweep — for typical queue sizes (≤ a few hundred) this is
# negligible memory.
_THUMB_CACHE: dict[str, "NSImage"] = {}
_THUMB_CACHE_MAX = 1024
_THUMB_LOCK = threading.Lock()


def _thumb_cache_get(key):
    with _THUMB_LOCK:
        return _THUMB_CACHE.get(key)


def _thumb_cache_put(key, image):
    if image is None:
        return
    with _THUMB_LOCK:
        if len(_THUMB_CACHE) >= _THUMB_CACHE_MAX:
            # Drop oldest ~10% of entries.
            for k in list(_THUMB_CACHE.keys())[: _THUMB_CACHE_MAX // 10]:
                _THUMB_CACHE.pop(k, None)
        _THUMB_CACHE[key] = image


def _make_image_thumbnail(path: pathlib.Path, max_pixel: int = 96):
    """Decode a downscaled thumbnail from a local image file (HEIC/JPEG/PNG/etc).

    Uses CGImageSourceCreateThumbnailAtIndex so we don't have to load the full
    image into memory. Returns an NSImage or None on failure.
    """
    try:
        url = NSURL.fileURLWithPath_(str(path))
        source = Quartz.CGImageSourceCreateWithURL(url, None)
        if source is None:
            return None
        options = {
            Quartz.kCGImageSourceCreateThumbnailFromImageAlways: True,
            Quartz.kCGImageSourceCreateThumbnailWithTransform: True,
            Quartz.kCGImageSourceThumbnailMaxPixelSize: int(max_pixel),
        }
        cg = Quartz.CGImageSourceCreateThumbnailAtIndex(source, 0, options)
        if cg is None:
            return None
        return NSImage.alloc().initWithCGImage_size_(cg, (max_pixel, max_pixel))
    except Exception:
        return None


def _make_video_thumbnail(path: pathlib.Path, max_pixel: int = 96):
    """Extract a single frame from a video and return it as an NSImage."""
    try:
        url = NSURL.fileURLWithPath_(str(path))
        asset = AVFoundation.AVAsset.assetWithURL_(url)
        if asset is None:
            return None
        gen = AVFoundation.AVAssetImageGenerator.alloc().initWithAsset_(asset)
        gen.setAppliesPreferredTrackTransform_(True)
        gen.setMaximumSize_((max_pixel * 2, max_pixel * 2))
        # Try a fraction of a second in; some videos have a black first frame.
        try_time = CoreMedia.CMTimeMakeWithSeconds(0.3, 600)
        result = gen.copyCGImageAtTime_actualTime_error_(try_time, None, None)
        # PyObjC returns (cgimg, actualTime, error)
        cg = result[0] if result else None
        if cg is None:
            return None
        return NSImage.alloc().initWithCGImage_size_(cg, (max_pixel, max_pixel))
    except Exception:
        return None


def _make_asset_thumbnail(asset, max_pixel: int = 96):
    """Get a thumbnail for a PHAsset (image or video) via PHImageManager."""
    try:
        from Photos import (
            PHImageContentModeAspectFill,
            PHImageManager,
            PHImageRequestOptions,
            PHImageRequestOptionsDeliveryModeHighQualityFormat,
        )
    except ImportError:
        return None
    options = PHImageRequestOptions.alloc().init()
    options.setSynchronous_(True)
    options.setNetworkAccessAllowed_(True)
    options.setDeliveryMode_(PHImageRequestOptionsDeliveryModeHighQualityFormat)
    result: dict = {}

    def handler(img, _info):
        if img is not None:
            result["img"] = img

    PHImageManager.defaultManager().requestImageForAsset_targetSize_contentMode_options_resultHandler_(
        asset,
        (max_pixel * 2, max_pixel * 2),
        PHImageContentModeAspectFill,
        options,
        handler,
    )
    return result.get("img")


def _thumb_key_for_path(path: pathlib.Path) -> str:
    return f"file:{path}"


def _thumb_key_for_asset(asset) -> str:
    return f"asset:{asset.localIdentifier()}"


# ---------- views ----------


class RemovableTableView(NSTableView):
    """NSTableView that calls the controller back when the user hits Delete /
    Backspace on the selected row(s). The controller's `removeSelectedRows_`
    method (set as the table's target) does the actual removal.
    """

    def keyDown_(self, event):
        chars = event.charactersIgnoringModifiers()
        if chars:
            code = ord(chars[0])
            # 0x7F = Delete (Forward Delete), 0x08 = Backspace
            if code in (0x7F, 0x08):
                target = self.target()
                action = self.action()
                if target is not None and action is not None:
                    target.performSelector_withObject_(action, self)
                    return
        objc.super(RemovableTableView, self).keyDown_(event)


class DropBaseView(NSVisualEffectView):
    """Base visual-effect view that accepts file drops and forwards to its controller."""

    def initWithFrame_controller_(self, frame, controller):
        self = objc.super(DropBaseView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._controller = controller
        self.registerForDraggedTypes_([NSPasteboardTypeFileURL])
        return self

    def draggingEntered_(self, sender):
        return self._controller.handleDraggingEntered_(sender)

    def draggingUpdated_(self, sender):
        return self._controller.handleDraggingEntered_(sender)

    def prepareForDragOperation_(self, _sender):
        return True

    def performDragOperation_(self, sender):
        return self._controller.handlePerformDrag_(sender)


class CenteredEmptyView(NSView):
    """Holds an icon + caption that stay centered when the panel resizes."""

    def initWithFrame_icon_label_(self, frame, icon_view, label_view):
        self = objc.super(CenteredEmptyView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._icon = icon_view
        self._label = label_view
        self.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.addSubview_(icon_view)
        self.addSubview_(label_view)
        self._relayout()
        return self

    def setFrameSize_(self, size):
        objc.super(CenteredEmptyView, self).setFrameSize_(size)
        self._relayout()

    @objc.python_method
    def _relayout(self):
        b = self.bounds()
        cx, cy = b.size.width / 2, b.size.height / 2
        self._icon.setFrame_(NSMakeRect(cx - 32, cy + 6, 64, 64))
        self._label.setFrame_(NSMakeRect(0, cy - 56, b.size.width, 36))


# ---------- model ----------


class PairItem:
    """One queued photo+video pair.

    Backed by either local paths (drag/drop, file picker) OR by PHAssets
    (library scan). `materialize(tmp_dir)` returns concrete paths on disk;
    for PHAsset-backed items it exports the resources first.
    """

    __slots__ = (
        "photo", "video", "status", "error", "asset_id",
        "photo_asset", "video_asset",
    )

    def __init__(
        self,
        photo: pathlib.Path,
        video: pathlib.Path,
        *,
        photo_asset=None,
        video_asset=None,
    ):
        self.photo = photo
        self.video = video
        self.status = "pending"
        self.error: str | None = None
        self.asset_id: str | None = None
        # Either both PHAssets are set (library-scan item) or both are None.
        self.photo_asset = photo_asset
        self.video_asset = video_asset

    @property
    def display_name(self) -> str:
        return self.photo.stem if self.photo.stem == self.video.stem else self.photo.name

    @property
    def is_library_backed(self) -> bool:
        return self.photo_asset is not None and self.video_asset is not None

    def materialize(self, tmp_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
        """Return (photo_path, video_path) materialised inside `tmp_dir`.

        For local-file pairs this is a copy. For PHAsset-backed pairs this
        exports the assets from Photos non-destructively.
        """
        from .library_scan import export_asset_to_file  # local import to avoid a cycle at module load

        if self.is_library_backed:
            pp = export_asset_to_file(self.photo_asset, tmp_dir)
            vp = export_asset_to_file(self.video_asset, tmp_dir)
            return pp, vp
        pp = tmp_dir / self.photo.name
        vp = tmp_dir / self.video.name
        shutil.copy(self.photo, pp)
        shutil.copy(self.video, vp)
        return pp, vp


# ---------- custom row views ----------


class SimpleFileRowView(NSView):
    """Lightweight row for the unpaired photo / video columns: thumbnail + filename."""

    def initWithFrame_(self, frame):
        self = objc.super(SimpleFileRowView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._icon = NSImageView.alloc().initWithFrame_(NSMakeRect(8, 4, 24, 24))
        self._icon.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        self._icon.setWantsLayer_(True)
        self._icon.layer().setCornerRadius_(4.0)
        self._icon.layer().setMasksToBounds_(True)
        self.addSubview_(self._icon)
        self._label = _label("", size=12)
        self._label.setFrame_(NSMakeRect(40, 6, frame.size.width - 48, 20))
        self._label.setAutoresizingMask_(NSViewWidthSizable)
        self.addSubview_(self._label)
        self._current_key = None
        return self

    @objc.python_method
    def configure(self, path: pathlib.Path):
        self._label.setStringValue_(path.name)
        key = _thumb_key_for_path(path)
        self._current_key = key

        # Cached thumbnail or placeholder + async load.
        cached = _thumb_cache_get(key)
        if cached is not None:
            self._icon.setImage_(cached)
            return
        placeholder = NSWorkspace.sharedWorkspace().iconForFile_(str(path))
        if placeholder is not None:
            placeholder.setSize_((24, 24))
        self._icon.setImage_(placeholder)

        is_video = is_video_file(path)

        def _load():
            img = (
                _make_video_thumbnail(path, max_pixel=64)
                if is_video
                else _make_image_thumbnail(path, max_pixel=64)
            )
            if img is None:
                return
            _thumb_cache_put(key, img)
            # Update only if this row hasn't been recycled to another item.
            if self._current_key != key:
                return
            _post_to_main(lambda i=img: self._icon.setImage_(i))

        threading.Thread(target=_load, daemon=True).start()


class PairRowView(NSView):
    """Custom NSTableView cell view: thumbnail + photo/video names + status badge."""

    def initWithFrame_(self, frame):
        self = objc.super(PairRowView, self).initWithFrame_(frame)
        if self is None:
            return None

        # Thumbnail
        self._thumb = NSImageView.alloc().initWithFrame_(NSMakeRect(12, 8, 40, 40))
        self._thumb.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        self._thumb.setWantsLayer_(True)
        self._thumb.layer().setCornerRadius_(6.0)
        self._thumb.layer().setMasksToBounds_(True)
        self.addSubview_(self._thumb)

        # Primary line (photo filename)
        self._photo_label = _label("", size=13, bold=True)
        self._photo_label.setFrame_(NSMakeRect(64, 28, frame.size.width - 200, 20))
        self._photo_label.setAutoresizingMask_(NSViewWidthSizable)
        self.addSubview_(self._photo_label)

        # Secondary line (video filename, dimmed)
        self._video_label = _label("", size=11, color=NSColor.secondaryLabelColor())
        self._video_label.setFrame_(NSMakeRect(64, 8, frame.size.width - 200, 16))
        self._video_label.setAutoresizingMask_(NSViewWidthSizable)
        self.addSubview_(self._video_label)

        # Status: icon + text, right-aligned
        self._status_icon = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 16, 16))
        self.addSubview_(self._status_icon)

        self._status_label = _label("", size=11, align=NSTextAlignmentRight)
        self.addSubview_(self._status_label)

        return self

    @objc.python_method
    def layout_status(self):
        bounds = self.bounds()
        # Position status group along the right edge.
        text_w = 96
        icon_w = 18
        gap = 4
        right = bounds.size.width - 16
        ty = (bounds.size.height - 16) / 2
        self._status_icon.setFrame_(
            NSMakeRect(right - text_w - gap - icon_w, ty, icon_w, 16)
        )
        self._status_label.setFrame_(
            NSMakeRect(right - text_w, (bounds.size.height - 16) / 2, text_w, 16)
        )

    def setFrameSize_(self, size):
        objc.super(PairRowView, self).setFrameSize_(size)
        self.layout_status()

    @objc.python_method
    def configure(self, pair: PairItem):
        self._photo_label.setStringValue_(pair.photo.name)
        if pair.status == "failed" and pair.error:
            err_one_line = " ".join(pair.error.split())
            self._video_label.setStringValue_(f"⚠ {err_one_line}")
            self._video_label.setTextColor_(NSColor.systemRedColor())
            # Hover tooltip carries the untruncated error.
            self.setToolTip_(pair.error)
        else:
            self._video_label.setStringValue_(f"↳ {pair.video.name}")
            self._video_label.setTextColor_(NSColor.secondaryLabelColor())
            self.setToolTip_(None)

        # Thumbnail: prefer the photo's real preview (decoded async). Library
        # assets use PHImageManager; local files use CGImageSourceThumbnail.
        if pair.is_library_backed:
            key = _thumb_key_for_asset(pair.photo_asset)
        else:
            key = _thumb_key_for_path(pair.photo)
        self._current_thumb_key = key

        cached = _thumb_cache_get(key)
        if cached is not None:
            self._thumb.setImage_(cached)
        else:
            # Placeholder while async loader runs.
            placeholder = _symbol("photo")
            if placeholder is not None:
                placeholder = placeholder.copy()
                placeholder.setSize_((40, 40))
            self._thumb.setImage_(placeholder)
            self._thumb.setContentTintColor_(NSColor.tertiaryLabelColor())

            if pair.is_library_backed:
                asset = pair.photo_asset
                def _load_asset():
                    img = _make_asset_thumbnail(asset, max_pixel=80)
                    if img is None:
                        return
                    _thumb_cache_put(key, img)
                    if self._current_thumb_key != key:
                        return
                    _post_to_main(lambda i=img: self._thumb.setImage_(i))
                threading.Thread(target=_load_asset, daemon=True).start()
            else:
                path = pair.photo
                def _load_file():
                    img = _make_image_thumbnail(path, max_pixel=80)
                    if img is None:
                        return
                    _thumb_cache_put(key, img)
                    if self._current_thumb_key != key:
                        return
                    _post_to_main(lambda i=img: self._thumb.setImage_(i))
                threading.Thread(target=_load_file, daemon=True).start()

        sym_name, color_name = STATUS_COLOR.get(pair.status, ("circle", "labelColor"))
        sym = _symbol(sym_name)
        if sym is not None:
            sym = sym.copy()
            self._status_icon.setImage_(sym)
        self._status_icon.setContentTintColor_(_color(color_name))
        self._status_label.setStringValue_(STATUS_TEXT.get(pair.status, pair.status))
        self._status_label.setTextColor_(_color(color_name))
        self.layout_status()


# ---------- main window controller ----------


class MainWindowController(NSObject):
    """All-in-one window controller: builds UI, owns the queue, drives the worker."""

    def init(self):
        self = objc.super(MainWindowController, self).init()
        if self is None:
            return None
        self._unpaired_photos: list[pathlib.Path] = []
        self._unpaired_videos: list[pathlib.Path] = []
        self._pairs: list[PairItem] = []
        self._processing = False
        self._build_window()
        return self

    # ----- window construction -----

    @objc.python_method
    def _build_window(self):
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
            | NSWindowStyleMaskFullSizeContentView
        )
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIN_W, WIN_H), style, NSBackingStoreBuffered, False
        )
        self._window.setTitle_("MakeLive")
        self._window.setTitlebarAppearsTransparent_(True)
        self._window.setTitleVisibility_(NSWindowTitleHidden)
        self._window.setMovableByWindowBackground_(True)
        self._window.setMinSize_((520, 380))
        self._window.center()

        # Frosted base view (subclassed to accept drops anywhere in window).
        base = DropBaseView.alloc().initWithFrame_controller_(
            self._window.contentView().bounds(), self
        )
        base.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        base.setMaterial_(NSVisualEffectMaterialUnderWindowBackground)
        base.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        base.setState_(NSVisualEffectStateActive)
        self._window.setContentView_(base)
        self._base = base

        # Build layout: header / toolbar / columns / pair-bar / queue / footer.
        self._build_header(base)
        self._build_toolbar(base)
        self._build_columns_area(base)
        self._build_pair_bar(base)
        self._build_queue_area(base)
        self._build_footer(base)
        self._refresh_buttons()

        self._window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    # Called from DropBaseView during drag operations.
    def handleDraggingEntered_(self, sender):
        urls = self._urls_from(sender)
        if self._has_acceptable_files(urls):
            return NSDragOperationCopy
        return NSDragOperationNone

    def handlePerformDrag_(self, sender):
        urls = self._urls_from(sender)
        if not urls:
            return False
        self._add_paths(urls)
        return True

    @objc.python_method
    def _urls_from(self, sender):
        pb = sender.draggingPasteboard()
        items = pb.readObjectsForClasses_options_([NSURL.class__()], None) or []
        return [pathlib.Path(str(u.path())) for u in items if u.isFileURL()]

    @objc.python_method
    def _has_acceptable_files(self, paths):
        return any(is_image_file(p) or is_video_file(p) for p in paths)

    @objc.python_method
    def _build_header(self, base):
        b = base.bounds()
        y_top = b.size.height
        title_h = 26
        sub_h = 16

        # Container so it slides with window resize.
        header = NSView.alloc().initWithFrame_(
            NSMakeRect(0, y_top - HEADER_H, b.size.width, HEADER_H)
        )
        header.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        base.addSubview_(header)

        title = _label("MakeLive", size=22, bold=True)
        title.setFrame_(NSMakeRect(EDGE_PAD, HEADER_H - TITLEBAR_INSET - title_h,
                                    b.size.width - 2 * EDGE_PAD, title_h))
        title.setAutoresizingMask_(NSViewWidthSizable)
        header.addSubview_(title)

        sub = _label(
            "Drop photo + video pairs anywhere. We’ll add them to Photos as Live Photos.",
            size=12,
            color=NSColor.secondaryLabelColor(),
        )
        sub.setFrame_(NSMakeRect(EDGE_PAD, 8, b.size.width - 2 * EDGE_PAD, sub_h))
        sub.setAutoresizingMask_(NSViewWidthSizable)
        header.addSubview_(sub)

    @objc.python_method
    def _build_toolbar(self, base):
        b = base.bounds()
        y = b.size.height - HEADER_H - TOOLBAR_H

        bar = NSView.alloc().initWithFrame_(NSMakeRect(0, y, b.size.width, TOOLBAR_H))
        bar.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        base.addSubview_(bar)

        # + Add Files…
        self._add_btn = self._make_button("Add Files…", "addFiles:", icon="plus")
        self._add_btn.setFrame_(NSMakeRect(EDGE_PAD, 10, 130, 28))
        bar.addSubview_(self._add_btn)

        # Clear
        self._clear_btn = self._make_button("Clear", "clearAll:", icon="trash")
        self._clear_btn.setFrame_(NSMakeRect(EDGE_PAD + 138, 10, 90, 28))
        bar.addSubview_(self._clear_btn)

        # Scan Photos library
        self._scan_btn = self._make_button(
            "Scan Library…", "scanLibrary:", icon="magnifyingglass"
        )
        self._scan_btn.setFrame_(NSMakeRect(EDGE_PAD + 236, 10, 150, 28))
        bar.addSubview_(self._scan_btn)

        # Right-aligned status summary
        self._summary = _label("No pairs queued", color=NSColor.tertiaryLabelColor(),
                               align=NSTextAlignmentRight)
        self._summary.setFrame_(NSMakeRect(
            b.size.width - EDGE_PAD - 240, 14, 240, 20
        ))
        # Stick to the right edge: the left margin grows with the window.
        self._summary.setAutoresizingMask_(NSViewMinXMargin)
        bar.addSubview_(self._summary)

    @objc.python_method
    def _make_inset_panel(self, base, frame, mask, corner=12.0):
        """Frosted rounded panel used as the background for the three table sections."""
        panel = NSVisualEffectView.alloc().initWithFrame_(frame)
        panel.setAutoresizingMask_(mask)
        panel.setMaterial_(NSVisualEffectMaterialContentBackground)
        panel.setBlendingMode_(NSVisualEffectBlendingModeWithinWindow)
        panel.setState_(NSVisualEffectStateActive)
        panel.setWantsLayer_(True)
        panel.layer().setCornerRadius_(corner)
        panel.layer().setMasksToBounds_(True)
        base.addSubview_(panel)
        return panel

    @objc.python_method
    def _make_simple_table(self, panel, identifier: str, *, header_text: str):
        """Create a scrollable single-column NSTableView for unpaired-file lists."""
        # Section label at top of the panel
        label_h = 22
        header = _label(header_text, size=11, bold=True, color=NSColor.secondaryLabelColor())
        header.setFrame_(NSMakeRect(10, panel.bounds().size.height - label_h - 2,
                                    panel.bounds().size.width - 20, label_h))
        header.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        panel.addSubview_(header)

        # Scroll view below the label
        scroll_y = 4
        scroll_h = panel.bounds().size.height - label_h - 6
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, scroll_y, panel.bounds().size.width, scroll_h)
        )
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        scroll.setHasVerticalScroller_(True)
        scroll.setDrawsBackground_(False)
        scroll.setBorderType_(0)
        panel.addSubview_(scroll)

        table = RemovableTableView.alloc().initWithFrame_(scroll.bounds())
        table.setRowHeight_(SIMPLE_ROW_HEIGHT)
        table.setHeaderView_(None)
        table.setBackgroundColor_(NSColor.clearColor())
        table.setAllowsMultipleSelection_(False)
        try:
            table.setStyle_(NSTableViewStyleInset)
        except Exception:
            pass
        table.setIdentifier_(identifier)
        table.setDataSource_(self)
        table.setDelegate_(self)
        # Delete-key handling: controller's removeRowsFromTable_ method reads
        # the table's selectedRowIndexes and deletes accordingly.
        table.setTarget_(self)
        table.setAction_("removeRowsFromTable:")
        table.setMenu_(self._build_remove_menu())

        col = NSTableColumn.alloc().initWithIdentifier_(identifier + "_col")
        col.setResizingMask_(1)
        col.setMinWidth_(120)
        table.addTableColumn_(col)
        scroll.setDocumentView_(table)
        return table

    @objc.python_method
    def _build_columns_area(self, base):
        b = base.bounds()
        # y starts below toolbar and runs upward
        top_y = b.size.height - HEADER_H - TOOLBAR_H - COLUMNS_H
        col_w = (b.size.width - 2 * EDGE_PAD - COL_GAP) / 2

        photos_panel = self._make_inset_panel(
            base,
            NSMakeRect(EDGE_PAD, top_y, col_w, COLUMNS_H),
            NSViewMinYMargin,  # stuck to top; bottom flexes (column area is fixed-height)
        )
        videos_panel = self._make_inset_panel(
            base,
            NSMakeRect(EDGE_PAD + col_w + COL_GAP, top_y, col_w, COLUMNS_H),
            NSViewMinYMargin | NSViewMinXMargin,
        )
        # Make panels grow horizontally as the window widens.
        photos_panel.setAutoresizingMask_(NSViewMinYMargin | NSViewWidthSizable)
        videos_panel.setAutoresizingMask_(NSViewMinYMargin | NSViewMinXMargin)

        self._photos_table = self._make_simple_table(
            photos_panel, "photos", header_text="PHOTOS (unpaired)"
        )
        self._videos_table = self._make_simple_table(
            videos_panel, "videos", header_text="VIDEOS (unpaired)"
        )

    @objc.python_method
    def _build_pair_bar(self, base):
        b = base.bounds()
        y = b.size.height - HEADER_H - TOOLBAR_H - COLUMNS_H - PAIR_BAR_H
        bar = NSView.alloc().initWithFrame_(NSMakeRect(0, y, b.size.width, PAIR_BAR_H))
        bar.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        base.addSubview_(bar)

        self._pair_btn = self._make_button(
            "Pair Selected", "pairSelected:", icon="arrow.left.and.right"
        )
        self._pair_btn.setFrame_(NSMakeRect((b.size.width - 200) / 2, 8, 200, 28))
        self._pair_btn.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxXMargin)
        bar.addSubview_(self._pair_btn)

    @objc.python_method
    def _build_queue_area(self, base):
        """Bottom section: paired queue (existing PairRowView)."""
        b = base.bounds()
        top = b.size.height - HEADER_H - TOOLBAR_H - COLUMNS_H - PAIR_BAR_H
        bottom = FOOTER_H
        panel = self._make_inset_panel(
            base,
            NSMakeRect(EDGE_PAD, bottom, b.size.width - 2 * EDGE_PAD, top - bottom - 4),
            NSViewWidthSizable | NSViewHeightSizable,
        )
        self._panel = panel

        # Section label
        label_h = 22
        header = _label("PAIRED QUEUE", size=11, bold=True, color=NSColor.secondaryLabelColor())
        header.setFrame_(NSMakeRect(10, panel.bounds().size.height - label_h - 2,
                                    panel.bounds().size.width - 20, label_h))
        header.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        panel.addSubview_(header)

        # Scroll view + queue table
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, 4, panel.bounds().size.width, panel.bounds().size.height - label_h - 6)
        )
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        scroll.setHasVerticalScroller_(True)
        scroll.setDrawsBackground_(False)
        scroll.setBorderType_(0)
        panel.addSubview_(scroll)

        table = RemovableTableView.alloc().initWithFrame_(scroll.bounds())
        table.setRowHeight_(ROW_HEIGHT)
        table.setHeaderView_(None)
        table.setBackgroundColor_(NSColor.clearColor())
        # Enable a subtle selection highlight so the user can see which rows
        # they're about to remove via the Delete key / context menu.
        try:
            from AppKit import NSTableViewSelectionHighlightStyleRegular
            table.setSelectionHighlightStyle_(NSTableViewSelectionHighlightStyleRegular)
        except Exception:
            pass
        table.setAllowsMultipleSelection_(True)
        try:
            table.setStyle_(NSTableViewStyleInset)
        except Exception:
            pass
        table.setIdentifier_("queue")
        table.setDataSource_(self)
        table.setDelegate_(self)
        table.setTarget_(self)
        table.setAction_("removeRowsFromTable:")
        table.setMenu_(self._build_remove_menu())
        col = NSTableColumn.alloc().initWithIdentifier_("pair")
        col.setResizingMask_(1)
        col.setMinWidth_(200)
        table.addTableColumn_(col)
        scroll.setDocumentView_(table)
        self._table = table

        # Empty-state overlay placed on top of the scroll view (sibling, added
        # last so it draws above). Hidden whenever there are queued pairs.
        icon = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 64, 64))
        sym = _symbol("photo.on.rectangle.angled")
        if sym is not None:
            icon.setImage_(sym)
        icon.setContentTintColor_(NSColor.tertiaryLabelColor())
        hint = _label(
            "Auto-paired files appear here.\nOr select one from each column above and click Pair.",
            size=13,
            color=NSColor.tertiaryLabelColor(),
            align=NSTextAlignmentCenter,
        )
        hint.setUsesSingleLineMode_(False)
        empty = CenteredEmptyView.alloc().initWithFrame_icon_label_(
            scroll.frame(), icon, hint
        )
        panel.addSubview_(empty)
        self._empty_overlay = empty

    @objc.python_method
    def _build_footer(self, base):
        b = base.bounds()
        footer = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, b.size.width, FOOTER_H))
        footer.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        base.addSubview_(footer)

        # Re-encode option (above the action button)
        self._reencode_checkbox = NSButton.alloc().init()
        self._reencode_checkbox.setButtonType_(NSButtonTypeSwitch)
        self._reencode_checkbox.setTitle_("Compress video to HEVC ~10 Mbps (preserves HDR)")
        self._reencode_checkbox.setState_(0)
        cb_w = 360
        self._reencode_checkbox.setFrame_(
            NSMakeRect((b.size.width - cb_w) / 2, FOOTER_H - 30, cb_w, 20)
        )
        self._reencode_checkbox.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxXMargin)
        footer.addSubview_(self._reencode_checkbox)

        self._process_btn = self._make_button(
            "Process All", "processAll:", primary=True
        )
        self._process_btn.setFrame_(NSMakeRect(
            (b.size.width - 220) / 2, 18, 220, 32
        ))
        self._process_btn.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxXMargin)
        self._process_btn.setKeyEquivalent_("\r")
        footer.addSubview_(self._process_btn)

    @objc.python_method
    def _make_button(self, title, action, *, primary=False, icon=None):
        b = NSButton.alloc().init()
        b.setTitle_(title)
        b.setBezelStyle_(NSBezelStyleRounded)
        b.setTarget_(self)
        b.setAction_(action)
        if icon is not None:
            sym = _symbol(icon)
            if sym is not None:
                b.setImage_(sym)
                b.setImagePosition_(2)  # NSImageLeading
        if primary:
            try:
                b.setKeyEquivalent_("\r")
                # macOS 11+: prominent default-button style
                b.setControlSize_(0)  # regular
            except Exception:
                pass
        return b

    # ----- actions -----

    @objc.IBAction
    def addFiles_(self, _sender):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(True)
        panel.setAllowedFileTypes_(["jpg", "jpeg", "heic", "heif", "mov", "mp4"])
        if panel.runModal() != 1:
            return
        paths = [pathlib.Path(str(u.path())) for u in panel.URLs()]
        self._add_paths(paths)

    @objc.IBAction
    def clearAll_(self, _sender):
        if self._processing:
            return
        self._pairs = []
        self._unpaired_photos = []
        self._unpaired_videos = []
        self._reload()

    @objc.python_method
    def _build_remove_menu(self):
        """Build the per-table context menu (single `Remove` item)."""
        menu = NSMenu.alloc().init()
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Remove", "removeRowsFromTable:", ""
        )
        item.setTarget_(self)
        menu.addItem_(item)
        return menu

    @objc.IBAction
    def removeRowsFromTable_(self, sender):
        """Remove the selected (or right-clicked) rows from whichever table sent
        the message. Wired both from the Delete-key path and the context menu.
        """
        if self._processing:
            return
        # `sender` is the NSTableView for delete-key calls. For context-menu
        # calls it's the NSMenuItem; recover the table via the menu's owning
        # view — clickedRow on each candidate table will be ≥0 for the one the
        # menu came from.
        table = None
        if hasattr(sender, "selectedRowIndexes"):
            table = sender
        else:
            for t in (self._table, self._photos_table, self._videos_table):
                if t.clickedRow() >= 0:
                    table = t
                    break
            if table is None:
                # Fall back to whichever table has focus / selection.
                for t in (self._table, self._photos_table, self._videos_table):
                    if t.selectedRow() >= 0:
                        table = t
                        break
        if table is None:
            return

        rows = list(table.selectedRowIndexes())
        # If user right-clicked an unselected row, include that one.
        clicked = table.clickedRow()
        if clicked >= 0 and clicked not in rows:
            rows = [clicked]
        if not rows:
            return
        rows.sort(reverse=True)
        role = self._table_role(table)
        target_list = {
            "photos": self._unpaired_photos,
            "videos": self._unpaired_videos,
        }.get(role, self._pairs)
        for r in rows:
            if 0 <= r < len(target_list):
                del target_list[r]
        self._reload()

    @objc.IBAction
    def scanLibrary_(self, _sender):
        if self._processing or getattr(self, "_scanning", False):
            return
        # Pre-scan options sheet (modal). Returns None if user cancels.
        from .scan_sheets import run_scan_options_alert

        options = run_scan_options_alert()
        if options is None:
            return

        self._scanning = True
        self._refresh_buttons()
        self._summary.setStringValue_("Scanning library…")
        self._summary.setTextColor_(NSColor.labelColor())
        threading.Thread(target=self._scan_worker, args=(options,), daemon=True).start()

    @objc.python_method
    def _scan_worker(self, options):
        # Local import: avoid pulling library_scan into module load.
        from .library_scan import (
            PhotosAccessError,
            _asset_original_filename,
            scan_library,
        )

        error = None
        candidates: list = []
        try:
            def progress(stage, done, total):
                txt = (
                    "Scanning library — fetching assets…"
                    if stage == "fetching"
                    else f"Filtering by camera ({done}/{total})…"
                )
                self._on_main(lambda t=txt: self._summary.setStringValue_(t))

            candidates = scan_library(
                window_seconds=options["window_seconds"],
                exclude_phones=options["exclude_phones"],
                date_from=options["date_from"],
                date_to=options["date_to"],
                max_video_duration=options.get("max_video_duration"),
                progress_cb=progress,
            )
        except PhotosAccessError as e:
            error = str(e)
        except Exception as e:
            print(traceback.format_exc())
            error = str(e)

        def finish():
            self._scanning = False
            if error is not None:
                self._summary.setStringValue_(f"Scan failed: {error}")
                self._summary.setTextColor_(NSColor.systemRedColor())
                self._refresh_buttons()
                return
            try:
                self._present_scan_results(candidates, _asset_original_filename)
            except Exception as exc:
                # Route the full traceback to NSLog (Console.app) and also to
                # /tmp/makelive_crash.log so we can grab it without spelunking.
                tb = traceback.format_exc()
                NSLog("[makelive] scan-results crash: %@", f"{exc}\n{tb}")
                try:
                    with open("/tmp/makelive_crash.log", "a") as fp:
                        fp.write(f"--- scan-results crash ---\n{exc}\n{tb}\n")
                except Exception:
                    pass
                self._summary.setStringValue_(f"Adding pairs failed: {exc}")
                self._summary.setTextColor_(NSColor.systemRedColor())
                self._refresh_buttons()

        self._on_main(finish)

    @objc.python_method
    def _present_scan_results(self, candidates, asset_filename_fn):
        """Group candidates by camera, ask user which to add, populate queue."""
        from .scan_sheets import run_camera_picker_alert

        if not candidates:
            # Use the sheet's own empty-case alert.
            run_camera_picker_alert([])
            self._summary.setStringValue_("Scan complete — no candidate pairs found.")
            self._summary.setTextColor_(NSColor.secondaryLabelColor())
            self._refresh_buttons()
            return

        # Group candidates by camera_label (one entry per distinct Make+Model).
        groups: dict[str, list] = {}
        for cand in candidates:
            groups.setdefault(cand.camera_label, []).append(cand)
        # Stable, deterministic order: by descending count, then label.
        ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        camera_counts = [(label, len(items)) for label, items in ordered]

        selected_labels = run_camera_picker_alert(camera_counts)
        if selected_labels is None:
            self._summary.setStringValue_(
                f"Scan found {len(candidates)} candidate pair"
                f"{'s' if len(candidates) != 1 else ''} — not added."
            )
            self._summary.setTextColor_(NSColor.secondaryLabelColor())
            self._refresh_buttons()
            return

        # Materialise selected pairs as PHAsset-backed PairItems.
        existing_ids = {
            (p.photo_asset.localIdentifier(), p.video_asset.localIdentifier())
            for p in self._pairs
            if p.is_library_backed
        }
        added = 0
        for label, items in ordered:
            if label not in selected_labels:
                continue
            for cand in items:
                key = (cand.photo.localIdentifier(), cand.video.localIdentifier())
                if key in existing_ids:
                    continue
                self._pairs.append(PairItem(
                    pathlib.Path(asset_filename_fn(cand.photo)),
                    pathlib.Path(asset_filename_fn(cand.video)),
                    photo_asset=cand.photo,
                    video_asset=cand.video,
                ))
                existing_ids.add(key)
                added += 1

        self._reload()
        self._summary.setStringValue_(
            f"Added {added} pair{'s' if added != 1 else ''} from library."
        )
        self._summary.setTextColor_(NSColor.systemGreenColor())

    @objc.IBAction
    def pairSelected_(self, _sender):
        if self._processing:
            return
        ph_row = self._photos_table.selectedRow()
        vd_row = self._videos_table.selectedRow()
        if ph_row < 0 or vd_row < 0:
            return
        if ph_row >= len(self._unpaired_photos) or vd_row >= len(self._unpaired_videos):
            return
        photo = self._unpaired_photos.pop(ph_row)
        video = self._unpaired_videos.pop(vd_row)
        self._pairs.append(PairItem(photo, video))
        self._reload()

    @objc.IBAction
    def processAll_(self, _sender):
        if self._processing:
            return
        pending = [p for p in self._pairs if p.status in ("pending", "failed")]
        if not pending:
            return
        self._processing = True
        # Reset failed→pending so the user can retry by clicking Process All.
        for p in pending:
            p.status = "pending"
            p.error = None
        self._refresh_buttons()
        self._reload()
        threading.Thread(target=self._work, args=(pending,), daemon=True).start()

    # ----- pairing / list management -----

    @objc.python_method
    def _add_paths(self, paths):
        """Add new files to the queue.

        Each incoming file is unioned with whatever is already sitting in the
        unpaired columns; then auto-pairing runs across the combined pool.
        Anything that pairs (by filename stem) is promoted to the queue; the
        rest stays in the columns for manual pairing.
        """
        # Files already queued, so we can deduplicate against them.
        in_pairs = set()
        for p in self._pairs:
            in_pairs.add(p.photo.resolve())
            in_pairs.add(p.video.resolve())

        # Build the candidate pool: existing unpaired + new (filtered to image/video).
        pool = list(self._unpaired_photos) + list(self._unpaired_videos)
        for p in paths:
            if not (is_image_file(p) or is_video_file(p)):
                continue
            try:
                rp = p.resolve()
            except Exception:
                rp = p
            if rp in in_pairs:
                continue
            if rp in {x.resolve() for x in pool}:
                continue
            pool.append(p)

        matched, unmatched = find_photo_video_pairs(pool)

        # Promote matched pairs into the queue.
        existing_keys = {(p.photo.resolve(), p.video.resolve()) for p in self._pairs}
        for image, video in matched:
            key = (image.resolve(), video.resolve())
            if key in existing_keys:
                continue
            self._pairs.append(PairItem(image, video))
            existing_keys.add(key)

        # Whatever didn't match goes back into the unpaired columns.
        self._unpaired_photos = [f for f in unmatched if is_image_file(f)]
        self._unpaired_videos = [f for f in unmatched if is_video_file(f)]

        self._reload()

    @objc.python_method
    def _reload(self):
        self._table.reloadData()
        self._photos_table.reloadData()
        self._videos_table.reloadData()
        self._empty_overlay.setHidden_(bool(self._pairs))
        self._refresh_buttons()
        self._update_summary()

    @objc.python_method
    def _refresh_buttons(self):
        has_any = bool(self._pairs) or self._unpaired_photos or self._unpaired_videos
        pending = any(p.status in ("pending", "failed") for p in self._pairs)
        busy = self._processing or getattr(self, "_scanning", False)
        self._clear_btn.setEnabled_(bool(has_any) and not busy)
        self._add_btn.setEnabled_(not busy)
        self._scan_btn.setEnabled_(not busy)
        self._process_btn.setEnabled_(pending and not busy)
        self._reencode_checkbox.setEnabled_(not busy)
        # Pair button is enabled iff exactly one row is selected in each column.
        ph = self._photos_table.selectedRow() if hasattr(self, "_photos_table") else -1
        vd = self._videos_table.selectedRow() if hasattr(self, "_videos_table") else -1
        self._pair_btn.setEnabled_(ph >= 0 and vd >= 0 and not busy)

    @objc.python_method
    def _update_summary(self):
        unpaired_n = len(self._unpaired_photos) + len(self._unpaired_videos)
        if not self._pairs and unpaired_n == 0:
            self._summary.setStringValue_("No files yet — drop or add some.")
            self._summary.setTextColor_(NSColor.tertiaryLabelColor())
            return
        if not self._pairs:
            self._summary.setStringValue_(
                f"{unpaired_n} unpaired — select one of each, then Pair"
            )
            self._summary.setTextColor_(NSColor.systemOrangeColor())
            return
        counts = {"pending": 0, "processing": 0, "added": 0, "failed": 0}
        for p in self._pairs:
            counts[p.status] = counts.get(p.status, 0) + 1
        parts = []
        if counts["added"]:
            parts.append(f"{counts['added']} added")
        if counts["processing"]:
            parts.append(f"{counts['processing']} in progress")
        if counts["pending"]:
            parts.append(f"{counts['pending']} pending")
        if counts["failed"]:
            parts.append(f"{counts['failed']} failed")
        self._summary.setStringValue_(" • ".join(parts) or f"{len(self._pairs)} pairs")
        self._summary.setTextColor_(
            NSColor.systemRedColor() if counts["failed"]
            else NSColor.secondaryLabelColor()
        )

    # ----- NSTableViewDataSource / NSTableViewDelegate -----

    @objc.python_method
    def _table_role(self, table) -> str:
        ident = table.identifier()
        return str(ident) if ident is not None else "queue"

    def numberOfRowsInTableView_(self, table):
        role = self._table_role(table)
        if role == "photos":
            return len(self._unpaired_photos)
        if role == "videos":
            return len(self._unpaired_videos)
        return len(self._pairs)

    def tableView_viewForTableColumn_row_(self, table, _column, row):
        role = self._table_role(table)
        if role in ("photos", "videos"):
            identifier = f"{role}_row"
            view = table.makeViewWithIdentifier_owner_(identifier, self)
            if view is None:
                view = SimpleFileRowView.alloc().initWithFrame_(
                    NSMakeRect(0, 0, 200, SIMPLE_ROW_HEIGHT)
                )
                view.setIdentifier_(identifier)
            items = self._unpaired_photos if role == "photos" else self._unpaired_videos
            view.configure(items[row])
            return view

        identifier = "pair_row"
        view = table.makeViewWithIdentifier_owner_(identifier, self)
        if view is None:
            view = PairRowView.alloc().initWithFrame_(NSMakeRect(0, 0, 600, ROW_HEIGHT))
            view.setIdentifier_(identifier)
        view.configure(self._pairs[row])
        return view

    def tableView_heightOfRow_(self, table, _row):
        role = self._table_role(table)
        if role in ("photos", "videos"):
            return SIMPLE_ROW_HEIGHT
        return ROW_HEIGHT

    def tableViewSelectionDidChange_(self, _notification):
        # When the user selects a row in either unpaired column, update the
        # Pair-Selected button's enabled state.
        self._refresh_buttons()

    # ----- worker -----

    @objc.python_method
    def _work(self, pending: list[PairItem]):
        # Snapshot the checkbox state once per Process All click; flipping it
        # mid-batch shouldn't change the in-flight semantics.
        reencode_enabled = self._reencode_checkbox.state() == NSControlStateValueOn
        try:
            for pair in pending:
                self._set_status_async(pair, "processing")
                stage = "preparing"
                try:
                    with tempfile.TemporaryDirectory(prefix="makelive_") as tmp:
                        tmp = pathlib.Path(tmp)
                        stage = "exporting from Photos" if pair.is_library_backed else "copying files"
                        pc, vc_initial = pair.materialize(tmp)
                        if reencode_enabled:
                            stage = "re-encoding video to HEVC"
                            vc = tmp / (pair.video.stem + ".mov")
                            if vc != vc_initial:
                                reencode_to_hevc(vc_initial, vc, bitrate=DEFAULT_HEVC_BITRATE)
                                vc_initial.unlink(missing_ok=True)
                            else:
                                tmp_out = tmp / (pair.video.stem + "_hevc.mov")
                                reencode_to_hevc(vc_initial, tmp_out, bitrate=DEFAULT_HEVC_BITRATE)
                                vc_initial.unlink(missing_ok=True)
                                tmp_out.rename(vc)
                        else:
                            vc = vc_initial
                        stage = "stamping ContentIdentifier"
                        pair.asset_id = make_live_photo(str(pc), str(vc))
                        stage = "importing to Photos"
                        import_live_photo(pc, vc)
                    self._set_status_async(pair, "added")
                except PhotosImportError as e:
                    pair.error = f"Photos import: {str(e).strip() or repr(e)}"
                    self._set_status_async(pair, "failed")
                except Exception as e:
                    msg = str(e).strip() or repr(e) or e.__class__.__name__
                    pair.error = f"{stage}: {msg}"
                    print(traceback.format_exc())
                    self._set_status_async(pair, "failed")
        finally:
            self._on_main(self._after_work)

    @objc.python_method
    def _set_status_async(self, pair: PairItem, status: str):
        pair.status = status
        try:
            row = self._pairs.index(pair)
        except ValueError:
            return
        self._on_main(lambda r=row: self._reload_row(r))

    @objc.python_method
    def _reload_row(self, row: int):
        if 0 <= row < len(self._pairs):
            from Foundation import NSIndexSet
            self._table.reloadDataForRowIndexes_columnIndexes_(
                NSIndexSet.indexSetWithIndex_(row),
                NSIndexSet.indexSetWithIndex_(0),
            )
        self._update_summary()

    @objc.python_method
    def _after_work(self):
        self._processing = False
        self._refresh_buttons()
        self._update_summary()

    @objc.python_method
    def _on_main(self, fn):
        _post_to_main(fn)
