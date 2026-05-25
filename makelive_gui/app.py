"""App entry point. The actual UI lives in window.py."""

from __future__ import annotations

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyRegular,
)
from Foundation import NSObject

from .window import MainWindowController


_delegate_ref = None  # keep a strong reference so AppKit doesn't deallocate


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, _notification):
        self._controller = MainWindowController.alloc().init()

    def applicationShouldTerminateAfterLastWindowClosed_(self, _sender):
        return True


def run():
    global _delegate_ref
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    _delegate_ref = AppDelegate.alloc().init()
    app.setDelegate_(_delegate_ref)
    app.run()


if __name__ == "__main__":
    run()
