# Amazon Photos Uploader

A small desktop app (Windows and macOS) that uploads a folder of photos to Amazon Photos, skipping anything already uploaded.

## Install

Download the latest build from the **Releases** page:

- **Windows:** `AmazonPhotosUploader.exe`
- **macOS:** `AmazonPhotosUploader-macos.zip` (unzip it)

Requires Google Chrome or Microsoft Edge to be installed.

### First launch (the app isn't code-signed)

- **Windows:** SmartScreen shows "Windows protected your PC". Click **More info → Run anyway**.
- **macOS:** Right-click the app → **Open** → **Open**. If macOS still refuses, go to System Settings → Privacy & Security and click **Open Anyway**.

You only need to do this once.

## Use

1. Click **Sign in**. A browser window opens: log in to Amazon (including any verification) and keep going until you can see your **Photos library**. The window closes by itself once you're there. Don't close it early, or the app won't know you're signed in.
2. **Add folder…** for each folder to upload (subfolders are included).
3. Click **Start upload**. Use "Preview only" first to see what would upload.

While uploading, the app drives a real Chrome/Edge window (a purple banner at the bottom of that window says so). **Leave that window open and don't click or type in it**, and keep the computer awake until it finishes. If it gets closed by accident, click **Start upload** again and it resumes where it left off.

Files are identified by their content (MD5), so re-running never uploads the same photo twice, and cancelling or crashing is safe.

## Run from source

    pip install -r requirements.txt
    python run_app.py

## Build

    pip install pyinstaller
    python build.py

Pushing a tag like `v1.0.0` makes GitHub Actions build both apps and publish them to a Release.
