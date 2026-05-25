"""Build the MakeLive.app bundle with py2app.

Usage:
    python setup.py py2app           # release bundle in dist/
    python setup.py py2app -A        # alias build (fast dev iteration)
"""

from setuptools import Distribution, setup

from makelive.version import __version__


class _AppDistribution(Distribution):
    """py2app rejects install_requires, which setuptools auto-derives from pyproject.toml.
    Strip it so the py2app command doesn't bail out."""

    def parse_config_files(self, *args, **kwargs):
        super().parse_config_files(*args, **kwargs)
        self.install_requires = None

APP = ["app_main.py"]

PLIST = {
    "CFBundleName": "MakeLive",
    "CFBundleDisplayName": "MakeLive",
    "CFBundleIdentifier": "com.rhettbull.makelive.gui",
    "CFBundleVersion": __version__,
    "CFBundleShortVersionString": __version__,
    "NSHighResolutionCapable": True,
    "LSMinimumSystemVersion": "10.15",
    "NSPhotoLibraryAddUsageDescription": (
        "MakeLive adds Live Photos to your Photos library."
    ),
    "NSPhotoLibraryUsageDescription": (
        "MakeLive needs access to add Live Photos to your Photos library."
    ),
}

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "asd.icns",
    "plist": PLIST,
    "packages": ["makelive", "makelive_gui"],
    "includes": [
        "AVFoundation",
        "AppKit",
        "Foundation",
        "Photos",
        "Quartz",
        "cgmetadata",
        "objc",
        "wurlitzer",
    ],
}

setup(
    app=APP,
    name="MakeLive",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
    distclass=_AppDistribution,
)
