# MakeLive

<!-- ALL-CONTRIBUTORS-BADGE:START - Do not remove or modify this section -->
[![All Contributors](https://img.shields.io/badge/all_contributors-2-orange.svg?style=flat-square)](#contributors-)
<!-- ALL-CONTRIBUTORS-BADGE:END -->

Convert a photo + video pair into an Apple Live Photo, and add it straight to your Photos library.

> This is a fork of [RhetTbull/makelive](https://github.com/RhetTbull/makelive). The fork adds:
> - a **macOS GUI app** with batch processing
> - a **byte-perfect HEIC pipeline** (HEVC bitstream is not re-encoded)
> - **HDR gain map and other auxiliary image preservation** on the JPEG path
>
> Library and CLI behaviour is otherwise identical to upstream. Pure CLI users may prefer the upstream package.

## Download

Grab the latest **MakeLive.app** from the [releases page](https://github.com/akvaithi/makelive/releases). Unzip, drag into `/Applications/`, and launch. Apple Silicon only.

The bundle is unsigned. On first launch macOS will warn; either right-click → Open, or clear the quarantine attribute:

```bash
xattr -dr com.apple.quarantine /Applications/MakeLive.app
```

## GUI

Drop any mix of photos and videos onto the window. Pairs are auto-matched by filename stem (`IMG_0001.heic` + `IMG_0001.mov`); unmatched files are reported in the summary line. Click **Process All** and each pair is stamped with a Live Photo `ContentIdentifier` and added to Photos via PhotoKit. Your original files are never modified — everything happens on temp copies.

- Liquid-glass-ish translucent window, adapts to system light / dark appearance
- Drop anywhere in the window (or use **Add Files…**)
- Sequential background worker with per-row status (Pending → Processing → Added / Failed)
- Direct PhotoKit import — no Photos.app jump

## CLI

The CLI behaves the same as upstream:

```bash
makelive IMG_1234.jpg IMG_1234.mov
```

See `makelive --help` for `--check`, `--pvt`, `--manual`, etc. Full CLI docs in upstream's README.

## Requirements

- macOS 10.15+ (GUI tested on macOS 14+, app bundle on macOS 26)
- Python 3.9+ (for installing from source; not needed for the .app)

## Installation

### macOS app (recommended)

Download from [releases](https://github.com/akvaithi/makelive/releases).

### From source

```bash
git clone https://github.com/akvaithi/makelive.git
cd makelive
uv venv && source .venv/bin/activate
uv pip install -e .
```

Run the CLI as `makelive ...` or the GUI as `python -m makelive_gui`. To produce a standalone `MakeLive.app`:

```bash
uv pip install py2app
python setup.py py2app          # standalone bundle in dist/
python setup.py py2app -A       # alias build for fast dev iteration
```

> If you hit `AttributeError: module 'zlib' has no attribute '__file__'` during the standalone build on Python 3.11+, patch `.venv/lib/python3.11/site-packages/py2app/build_app.py` around line 2443 to skip `zlib` when `__file__` is missing — it's been statically linked into CPython since 3.11.

## API

```python
from makelive import make_live_photo

asset_id = make_live_photo("test.heic", "test.mov")
print(f"Wrote asset ID: {asset_id}")
```

Check whether a pair is already a Live Photo, and read back its identifier:

```python
from makelive import is_live_photo_pair, live_id

print(is_live_photo_pair("test.heic", "test.mov"))   # asset_id or False
print(live_id("test.heic"))                          # asset_id or None
```

Save a `.pvt` package (preserves originals; the package can be double-clicked into Photos):

```python
from makelive import save_live_photo_pair_as_pvt

asset_id, pvt_path = save_live_photo_pair_as_pvt("test.heic", "test.mov")
```

## How it works

To register as a Live Photo, the photo's MakerNote and the video's QuickTime metadata must both carry the same `ContentIdentifier` UUID. The MakerNote field cannot be created by tools like `exiftool` if it doesn't already exist.

**Video side** — `AVMutableMovie.writeMovieHeaderToURL_` rewrites only the movie header for `.mov`; track data and `cdsc`/`cdep` tref atoms (needed for Live Wallpaper) are preserved. `.mp4` falls back to `AVAssetExportSession` with the passthrough preset.

**Photo side** — split by format:

| Format | Path | Pixel data | Notes |
|---|---|---|---|
| **HEIC / HEIF** | [`heic_metadata.py`](makelive/heic_metadata.py) — ISOBMFF box surgery | **byte-identical** | Adds ~100 bytes for the new MakerNote. HDR gain map / depth / auxiliary items preserved trivially because `mdat` isn't touched. |
| **JPEG** | `CGImageDestinationAddImageFromSource` (default quality = passthrough) | **byte-identical** | `kCGImageDestinationPreserveGainMap` set; depth / portrait / segmentation mattes re-attached explicitly via `CGImageDestinationAddAuxiliaryDataInfo`. |

The HEIC path bypasses Core Graphics entirely because `CGImageDestinationCopyImageSource` (the lossless CG API) can't write MakerNote, while `AddImageFromSource` (which can) always re-encodes HEVC. So the fork walks the file's ISOBMFF box tree, finds the `Exif` item via `iinf` + `iloc`, injects an Apple-format MakerNote with [`piexif`](https://pypi.org/project/piexif/), appends the new EXIF blob into the trailing `mdat` (extending the box's size header so no bytes sit outside any box — strict readers like Adobe Camera Raw reject orphaned trailing data), and updates only the EXIF item's `iloc` extent.

Verified on iPhone HEICs and the bundled `tests/test2.heic`. Files outside the validated shape (multi-extent EXIF, missing EXIF item, non-trailing `mdat`, `construction_method != 0`) raise `NotImplementedError` with a clear message rather than silently corrupting.

## Caveats

- **Apple Silicon only** for the bundled .app. From-source install works on Intel Macs but isn't tested as thoroughly.
- The .app is **unsigned and unnotarised**.
- XMP metadata in QuickTime movies isn't preserved on the `.mp4` path (this is an upstream behaviour and applies to videos only).
- The byte-perfect HEIC path assumes the source has a single `Exif` item with one extent and the `mdat` is the trailing top-level box. True for iPhone and Android HEICs in the wild; raises a clear error otherwise.

## Project layout

```
makelive/
├── makelive/
│   ├── makelive.py        # public API + JPEG / video paths
│   └── heic_metadata.py   # NEW — byte-perfect HEIC injection
├── makelive_gui/          # NEW — Cocoa GUI app
├── setup.py + app_main.py # NEW — py2app build for MakeLive.app
└── tests/
```

## Upstream

Core library improvements (HEIC byte-perfect path + aux preservation) are submitted upstream as [RhetTbull/makelive#35](https://github.com/RhetTbull/makelive/pull/35). The GUI lives only in this fork.

## License

MIT. See [LICENSE](LICENSE).

## Credits

- Upstream project by [@RhetTbull](https://github.com/RhetTbull) — see [original README](https://github.com/RhetTbull/makelive#readme).
- [Live-Photo-master](https://github.com/GUIYIVIEW/LivePhoto-master) by [@GUIYIVIEW](https://github.com/GUIYIVIEW) for the asset-id-in-QuickTime technique.
- [@Yorian](https://github.com/Yorian) for proposing the original project and test images.

## Contributors ✨

Thanks goes to these wonderful people ([emoji key](https://allcontributors.org/docs/en/emoji-key)):

<!-- ALL-CONTRIBUTORS-LIST:START - Do not remove or modify this section -->
<!-- prettier-ignore-start -->
<!-- markdownlint-disable -->
<table>
  <tbody>
    <tr>
      <td align="center" valign="top" width="14.28%"><a href="http://am1006.me"><img src="https://avatars.githubusercontent.com/u/13403435?v=4?s=100" width="100px;" alt="Luitbald"/><br /><sub><b>Luitbald</b></sub></a><br /><a href="https://github.com/RhetTbull/makelive/commits?author=am1006" title="Code">💻</a></td>
      <td align="center" valign="top" width="14.28%"><a href="https://github.com/Jaqobs"><img src="https://avatars.githubusercontent.com/u/39723475?v=4?s=100" width="100px;" alt="Jaqobs"/><br /><sub><b>Jaqobs</b></sub></a><br /><a href="https://github.com/RhetTbull/makelive/commits?author=Jaqobs" title="Code">💻</a></td>
    </tr>
  </tbody>
</table>

<!-- markdownlint-restore -->
<!-- prettier-ignore-end -->

<!-- ALL-CONTRIBUTORS-LIST:END -->

This project follows the [all-contributors](https://github.com/all-contributors/all-contributors) specification.
