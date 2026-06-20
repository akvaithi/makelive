# CLAUDE.md

Guidance for working in this repo.

## What this is
Converts a photo + video pair into an Apple **Live Photo** and adds it to the Photos
library. This is a fork of [RhetTbull/makelive](https://github.com/RhetTbull/makelive)
that adds, on top of the upstream CLI/library:
- a **macOS GUI app** with batch drag-and-drop processing (`makelive_gui/`),
- a **byte-perfect HEIC pipeline** (the HEVC bitstream is not re-encoded),
- **HDR gain map / auxiliary image preservation** on the JPEG path.

Library and CLI behaviour is otherwise identical to upstream. Apple Silicon only.

## Key files / layout
- `makelive/` — the library + CLI:
  - `makelive.py` — core: pairs a photo + video, writes the Live Photo
    `ContentIdentifier` to both, the public API.
  - `heic_metadata.py` — byte-perfect HEIC metadata writing (no re-encode).
  - `mp4_metadata.py` — QuickTime/MP4 metadata for the video side.
  - `video_reencode.py` — fallback re-encode path for incompatible video.
  - `__main__.py` — `click` CLI entry point. `version.py` — single source of version.
- `makelive_gui/` — the SwiftUI-feel PyObjC GUI:
  - `app.py` / `window.py` — app + main window (drag-drop, per-row status worker).
  - `photos.py` — PhotoKit import. `library_scan.py` / `scan_sheets.py` — library scan.
  - `__main__.py` — GUI entry point.
- `app_main.py`, `build.sh`, `applecrate.toml`, `setup.py` — `.app` packaging (py2app).
- `tests/test_makelive.py` + fixtures (`test.jpeg`, `test.mov`, `test2.heic`, …).

## Commands
```bash
# dev install (editable, with test/lint/app extras)
python -m pip install -e ".[test,lint,dev,app]"

# CLI
makelive IMG_1234.jpg IMG_1234.mov

# GUI (from source)
python -m makelive_gui

# tests / lint / types
python -m pytest                 # needs the macOS frameworks (pyobjc)
ruff check .
mypy makelive

# build the macOS .app
./build.sh                       # py2app → dist/ ; applecrate for the installer
```

## Conventions / gotchas
- **macOS-only** and **Apple Silicon-only** — depends on pyobjc + PhotoKit/AVFoundation;
  tests won't run off a Mac.
- The HEIC path is **byte-perfect by design**: don't introduce a re-encode on the
  HEVC bitstream or you lose the upstream-distinguishing feature. `video_reencode.py`
  is only the fallback for incompatible input.
- Originals are never modified — processing happens on temp copies, then PhotoKit import.
- Versioning is driven by `.bumpversion.cfg` (`bump2version`); the bundle is unsigned
  (users clear quarantine with `xattr -dr com.apple.quarantine`).
- Keep library/CLI behaviour in parity with upstream; the fork's value-add is the GUI
  and the byte-perfect/HDR pipelines, not divergent core behaviour.
