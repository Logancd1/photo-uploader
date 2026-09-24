"""
Build a standalone app with PyInstaller (run on the OS you're targeting).

    pip install -r requirements.txt pyinstaller
    python build.py

Output: dist/AmazonPhotosUploader.exe (Windows) or dist/AmazonPhotosUploader.app (macOS).
The app uses the user's installed Chrome/Edge, so no browser is bundled.
"""
import sys

import PyInstaller.__main__

args = [
    "run_app.py",
    "--name", "AmazonPhotosUploader",
    "--windowed",
    "--noconfirm",
    "--collect-all", "playwright",  # ships the Node driver Playwright needs at runtime
]
# macOS .app bundles must be onedir; Windows is friendlier as a single exe.
args.append("--onedir" if sys.platform == "darwin" else "--onefile")
PyInstaller.__main__.run(args)
