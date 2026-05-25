"""Main window: batch Live-Photo creator with a Liquid-Glass-ish appearance."""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import threading
import traceback

import objc
from AppKit import (
    NSApp,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSDragOperationCopy,
    NSDragOperationNone,
    NSFont,
    NSImageScaleProportionallyUpOrDown,
    NSImageView,
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

from .photos import PhotosImportError, import_live_photo


# ---------- constants / layout ----------

WIN_W, WIN_H = 720, 540
TITLEBAR_INSET = 28              # height we reserve for traffic-light buttons
HEADER_H = 78
TOOLBAR_H = 52
FOOTER_H = 64
ROW_HEIGHT = 56
EDGE_PAD = 16

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


# ---------- views ----------


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
    __slots__ = ("photo", "video", "status", "error", "asset_id")

    def __init__(self, photo: pathlib.Path, video: pathlib.Path):
        self.photo = photo
        self.video = video
        self.status = "pending"
        self.error: str | None = None
        self.asset_id: str | None = None

    @property
    def display_name(self) -> str:
        # If stems match (the common case), show the stem; otherwise show both
        # filenames.
        return self.photo.stem if self.photo.stem == self.video.stem else self.photo.name


# ---------- custom row view ----------


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
        self._video_label.setStringValue_(f"↳ {pair.video.name}")

        # Use the QuickLook / Finder icon for the photo as a cheap thumbnail.
        icon = NSWorkspace.sharedWorkspace().iconForFile_(str(pair.photo))
        if icon is not None:
            icon.setSize_((40, 40))
        self._thumb.setImage_(icon)

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

        # Build layout: header / toolbar / table / footer.
        self._build_header(base)
        self._build_toolbar(base)
        self._build_table_area(base)
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
    def _build_table_area(self, base):
        b = base.bounds()
        top = b.size.height - HEADER_H - TOOLBAR_H
        bottom = FOOTER_H
        # Inset panel with a slightly different material so text reads cleanly.
        panel = NSVisualEffectView.alloc().initWithFrame_(
            NSMakeRect(EDGE_PAD, bottom, b.size.width - 2 * EDGE_PAD, top - bottom - 8)
        )
        panel.setAutoresizingMask_(
            NSViewWidthSizable | NSViewHeightSizable
        )
        panel.setMaterial_(NSVisualEffectMaterialContentBackground)
        panel.setBlendingMode_(NSVisualEffectBlendingModeWithinWindow)
        panel.setState_(NSVisualEffectStateActive)
        panel.setWantsLayer_(True)
        panel.layer().setCornerRadius_(12.0)
        panel.layer().setMasksToBounds_(True)
        base.addSubview_(panel)
        self._panel = panel

        # Scroll view + table fill the panel.
        scroll = NSScrollView.alloc().initWithFrame_(panel.bounds())
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        scroll.setHasVerticalScroller_(True)
        scroll.setDrawsBackground_(False)
        scroll.setBorderType_(0)
        panel.addSubview_(scroll)

        table = NSTableView.alloc().initWithFrame_(panel.bounds())
        table.setRowHeight_(ROW_HEIGHT)
        table.setHeaderView_(None)
        table.setBackgroundColor_(NSColor.clearColor())
        table.setSelectionHighlightStyle_(NSTableViewSelectionHighlightStyleNone)
        try:
            table.setStyle_(NSTableViewStyleInset)
        except Exception:
            pass
        table.setDataSource_(self)
        table.setDelegate_(self)

        col = NSTableColumn.alloc().initWithIdentifier_("pair")
        col.setResizingMask_(1)  # NSTableColumnAutoresizingMask
        col.setMinWidth_(200)
        table.addTableColumn_(col)

        scroll.setDocumentView_(table)
        self._table = table

        # Empty-state overlay (centered "drop files here" hint).
        icon = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 64, 64))
        sym = _symbol("photo.on.rectangle.angled")
        if sym is not None:
            icon.setImage_(sym)
        icon.setContentTintColor_(NSColor.tertiaryLabelColor())

        hint = _label(
            "Drop photos and videos here\n(JPEG / HEIC + MOV / MP4)",
            size=13,
            color=NSColor.tertiaryLabelColor(),
            align=NSTextAlignmentCenter,
        )
        hint.setUsesSingleLineMode_(False)

        empty = CenteredEmptyView.alloc().initWithFrame_icon_label_(
            panel.bounds(), icon, hint
        )
        panel.addSubview_(empty)
        self._empty_overlay = empty

    @objc.python_method
    def _build_footer(self, base):
        b = base.bounds()
        footer = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, b.size.width, FOOTER_H))
        footer.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        base.addSubview_(footer)

        self._process_btn = self._make_button(
            "Process All", "processAll:", primary=True
        )
        self._process_btn.setFrame_(NSMakeRect(
            (b.size.width - 220) / 2, (FOOTER_H - 32) / 2, 220, 32
        ))
        # Centered horizontally: both side margins flex.
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
        matched, unmatched = find_photo_video_pairs(paths)
        existing = {(p.photo.resolve(), p.video.resolve()) for p in self._pairs}
        added = 0
        for image, video in matched:
            key = (image.resolve(), video.resolve())
            if key in existing:
                continue
            self._pairs.append(PairItem(image, video))
            existing.add(key)
            added += 1
        self._reload()
        if unmatched:
            names = ", ".join(p.name for p in unmatched[:3])
            extra = f" (+{len(unmatched)-3} more)" if len(unmatched) > 3 else ""
            self._summary.setStringValue_(
                f"Added {added} • Ignored {len(unmatched)} unmatched: {names}{extra}"
            )
            self._summary.setTextColor_(NSColor.systemOrangeColor())
        else:
            self._update_summary()

    @objc.python_method
    def _reload(self):
        self._table.reloadData()
        self._empty_overlay.setHidden_(bool(self._pairs))
        self._refresh_buttons()
        self._update_summary()

    @objc.python_method
    def _refresh_buttons(self):
        has_any = bool(self._pairs)
        pending = any(p.status in ("pending", "failed") for p in self._pairs)
        self._clear_btn.setEnabled_(has_any and not self._processing)
        self._add_btn.setEnabled_(not self._processing)
        self._process_btn.setEnabled_(pending and not self._processing)

    @objc.python_method
    def _update_summary(self):
        if not self._pairs:
            self._summary.setStringValue_("No pairs queued")
            self._summary.setTextColor_(NSColor.tertiaryLabelColor())
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

    def numberOfRowsInTableView_(self, _table):
        return len(self._pairs)

    def tableView_viewForTableColumn_row_(self, table, _column, row):
        identifier = "pair_row"
        view = table.makeViewWithIdentifier_owner_(identifier, self)
        if view is None:
            view = PairRowView.alloc().initWithFrame_(NSMakeRect(0, 0, 600, ROW_HEIGHT))
            view.setIdentifier_(identifier)
        view.configure(self._pairs[row])
        return view

    def tableView_heightOfRow_(self, _table, _row):
        return ROW_HEIGHT

    # ----- worker -----

    @objc.python_method
    def _work(self, pending: list[PairItem]):
        try:
            for pair in pending:
                self._set_status_async(pair, "processing")
                try:
                    with tempfile.TemporaryDirectory(prefix="makelive_") as tmp:
                        tmp = pathlib.Path(tmp)
                        pc = tmp / pair.photo.name
                        vc = tmp / pair.video.name
                        shutil.copy(pair.photo, pc)
                        shutil.copy(pair.video, vc)
                        pair.asset_id = make_live_photo(str(pc), str(vc))
                        import_live_photo(pc, vc)
                    self._set_status_async(pair, "added")
                except PhotosImportError as e:
                    pair.error = str(e)
                    self._set_status_async(pair, "failed")
                except Exception as e:
                    print(traceback.format_exc())
                    pair.error = str(e)
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
        NSOperationQueue.mainQueue().addOperationWithBlock_(fn)
