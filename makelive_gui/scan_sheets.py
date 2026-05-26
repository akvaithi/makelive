"""Modal sheets for the library-scan flow.

Two app-modal NSAlerts:

  `run_scan_options_alert()`  — pre-scan: pick a date range, the time
      window, and the exclude-phones toggle.
  `run_camera_picker_alert()` — post-scan: list the cameras found in the
      scan results with per-camera pair counts, let the user pick which to
      add to the queue.

Both return `None` if the user cancels. Keeping these as NSAlerts (rather
than full NSWindow sheets attached to the main window) makes the code
~80% smaller; the trade-off is that they're app-modal rather than
window-modal.
"""

from __future__ import annotations

from typing import Optional

import objc
from Foundation import NSObject
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSBezelStyleRounded,
    NSButton,
    NSButtonTypeSwitch,
    NSColor,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSDatePicker,
    NSDatePickerElementFlagYearMonthDay,
    NSDatePickerStyleTextFieldAndStepper,
    NSFont,
    NSScrollView,
    NSTextAlignmentLeft,
    NSTextField,
    NSView,
    NSViewHeightSizable,
    NSViewWidthSizable,
)
from Foundation import NSDate, NSMakeRect


# ---------- small helpers (mirroring window.py's _label etc.) ----------


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
    if color is not None:
        f.setTextColor_(color)
    return f


class _ToggleEnabler(NSObject):
    """Small target object: wires an NSButton's state to a list of dependent
    controls' enabled state. `invert=True` flips the relationship (so a
    toggle whose ON means "ignore the dependents" disables them).
    """

    def initWithDependents_invert_(self, dependents, invert):
        self = objc.super(_ToggleEnabler, self).init()
        if self is None:
            return None
        self._dependents = list(dependents)
        self._invert = bool(invert)
        return self

    def toggle_(self, sender):
        is_on = sender.state() == NSControlStateValueOn
        enable = (not is_on) if self._invert else is_on
        for d in self._dependents:
            d.setEnabled_(enable)


def _date_picker(default_date) -> NSDatePicker:
    p = NSDatePicker.alloc().init()
    p.setDatePickerStyle_(NSDatePickerStyleTextFieldAndStepper)
    p.setDatePickerElements_(NSDatePickerElementFlagYearMonthDay)
    p.setDateValue_(default_date)
    return p


# ---------- pre-scan: options ----------


def run_scan_options_alert(
    *,
    default_window_minutes: int = 5,
    default_exclude_phones: bool = True,
    default_max_video_seconds: int = 20,
) -> Optional[dict]:
    """Show the scan-options alert. Returns the chosen options dict, or None
    if cancelled.

    Returns dict keys:
        - date_from (NSDate or None — None == no lower bound)
        - date_to   (NSDate or None — None == no upper bound)
        - window_seconds (float)
        - exclude_phones (bool)
        - max_video_duration (float or None — None == no duration cap)
    """
    alert = NSAlert.alloc().init()
    alert.setMessageText_("Scan Photos Library")
    alert.setInformativeText_(
        "Find photo + video pairs taken close together. Videos from phones "
        "are fine — only photos from phones are excluded by default."
    )

    accessory = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 480, 176))

    # "All dates" toggle
    all_dates = NSButton.alloc().initWithFrame_(NSMakeRect(0, 148, 200, 22))
    all_dates.setButtonType_(NSButtonTypeSwitch)
    all_dates.setTitle_("All dates")
    all_dates.setState_(NSControlStateValueOn)
    accessory.addSubview_(all_dates)

    # Date range row (initially disabled)
    today = NSDate.date()
    six_months = today.dateByAddingTimeInterval_(-180 * 24 * 3600)
    from_label = _label("From:", size=12)
    from_label.setFrame_(NSMakeRect(20, 118, 50, 22))
    accessory.addSubview_(from_label)
    from_picker = _date_picker(six_months)
    from_picker.setFrame_(NSMakeRect(60, 118, 160, 22))
    accessory.addSubview_(from_picker)

    to_label = _label("To:", size=12)
    to_label.setFrame_(NSMakeRect(240, 118, 40, 22))
    accessory.addSubview_(to_label)
    to_picker = _date_picker(today)
    to_picker.setFrame_(NSMakeRect(270, 118, 160, 22))
    accessory.addSubview_(to_picker)

    from_picker.setEnabled_(False)
    to_picker.setEnabled_(False)

    # Wire the All-dates toggle: pickers are enabled when the toggle is OFF.
    dates_enabler = _ToggleEnabler.alloc().initWithDependents_invert_(
        [from_picker, to_picker], True
    )
    all_dates.setTarget_(dates_enabler)
    all_dates.setAction_("toggle:")

    # Pair window (gap between photo and video creation times)
    win_label = _label("Pair window:", size=12)
    win_label.setFrame_(NSMakeRect(0, 82, 100, 22))
    accessory.addSubview_(win_label)
    win_field = NSTextField.alloc().initWithFrame_(NSMakeRect(100, 82, 60, 22))
    win_field.setStringValue_(str(default_window_minutes))
    accessory.addSubview_(win_field)
    win_unit = _label("minutes", size=12, color=NSColor.secondaryLabelColor())
    win_unit.setFrame_(NSMakeRect(165, 82, 80, 22))
    accessory.addSubview_(win_unit)

    # Max video duration (skip clips longer than this)
    max_check = NSButton.alloc().initWithFrame_(NSMakeRect(0, 50, 170, 22))
    max_check.setButtonType_(NSButtonTypeSwitch)
    max_check.setTitle_("Max video length:")
    max_check.setState_(NSControlStateValueOn)
    accessory.addSubview_(max_check)
    max_field = NSTextField.alloc().initWithFrame_(NSMakeRect(170, 50, 60, 22))
    max_field.setStringValue_(str(default_max_video_seconds))
    accessory.addSubview_(max_field)
    max_unit = _label("seconds", size=12, color=NSColor.secondaryLabelColor())
    max_unit.setFrame_(NSMakeRect(235, 50, 80, 22))
    accessory.addSubview_(max_unit)

    # Wire the Max-video-length toggle: the seconds field tracks the toggle.
    max_enabler = _ToggleEnabler.alloc().initWithDependents_invert_(
        [max_field], False
    )
    max_check.setTarget_(max_enabler)
    max_check.setAction_("toggle:")

    # Exclude phones toggle
    excl_check = NSButton.alloc().initWithFrame_(NSMakeRect(0, 14, 400, 22))
    excl_check.setButtonType_(NSButtonTypeSwitch)
    excl_check.setTitle_("Exclude photos taken by phones")
    excl_check.setState_(
        NSControlStateValueOn if default_exclude_phones else NSControlStateValueOff
    )
    accessory.addSubview_(excl_check)

    alert.setAccessoryView_(accessory)
    alert.addButtonWithTitle_("Scan")
    alert.addButtonWithTitle_("Cancel")

    response = alert.runModal()
    if response != NSAlertFirstButtonReturn:
        return None

    # Parse options.
    use_dates = all_dates.state() == NSControlStateValueOff
    try:
        window_minutes = float(win_field.stringValue())
    except ValueError:
        window_minutes = float(default_window_minutes)
    use_max = max_check.state() == NSControlStateValueOn
    if use_max:
        try:
            max_seconds: Optional[float] = max(0.1, float(max_field.stringValue()))
        except ValueError:
            max_seconds = float(default_max_video_seconds)
    else:
        max_seconds = None
    return {
        "date_from": from_picker.dateValue() if use_dates else None,
        "date_to": to_picker.dateValue() if use_dates else None,
        "window_seconds": max(1.0, window_minutes * 60.0),
        "exclude_phones": excl_check.state() == NSControlStateValueOn,
        "max_video_duration": max_seconds,
    }


# ---------- post-scan: camera picker ----------


def run_camera_picker_alert(camera_counts: list[tuple[str, int]]) -> Optional[set[str]]:
    """Show the camera-picker alert. Each entry is `(camera_label, count)`.

    Returns the set of selected camera_labels, or None if cancelled. By
    default every camera is checked.
    """
    if not camera_counts:
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Scan complete")
        alert.setInformativeText_("No camera pairs found in that date range.")
        alert.addButtonWithTitle_("OK")
        alert.runModal()
        return None

    alert = NSAlert.alloc().init()
    total = sum(c for _, c in camera_counts)
    alert.setMessageText_(f"Found {total} candidate pair{'s' if total != 1 else ''}")
    alert.setInformativeText_(
        "Pick which cameras' pairs to add to the queue. "
        "Unchecked cameras are skipped (originals stay untouched)."
    )

    # Build a scrollable list of checkboxes (one per camera).
    row_h = 24
    list_height = min(280, max(60, row_h * len(camera_counts) + 8))
    container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 360, list_height))
    y = list_height - row_h
    checkboxes: list[tuple[NSButton, str]] = []
    for label, count in camera_counts:
        cb = NSButton.alloc().initWithFrame_(NSMakeRect(8, y, 344, row_h))
        cb.setButtonType_(NSButtonTypeSwitch)
        cb.setTitle_(f"{label}   ({count} pair{'s' if count != 1 else ''})")
        cb.setState_(NSControlStateValueOn)
        container.addSubview_(cb)
        checkboxes.append((cb, label))
        y -= row_h

    if list_height < row_h * len(camera_counts) + 8:
        # Wrap in a scroll view if it overflows.
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 360, list_height))
        scroll.setHasVerticalScroller_(True)
        scroll.setDrawsBackground_(False)
        scroll.setBorderType_(0)
        container.setFrame_(NSMakeRect(0, 0, 344, row_h * len(camera_counts) + 8))
        scroll.setDocumentView_(container)
        accessory = scroll
    else:
        accessory = container

    alert.setAccessoryView_(accessory)
    alert.addButtonWithTitle_("Add Selected")
    alert.addButtonWithTitle_("Cancel")

    response = alert.runModal()
    if response != NSAlertFirstButtonReturn:
        return None

    return {label for cb, label in checkboxes if cb.state() == NSControlStateValueOn}
