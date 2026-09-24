#!/usr/bin/env python3
"""
One-time interactive login for the browser-based Amazon Photos uploader.

Opens a real, visible Chromium window. You log in to Amazon by hand (this
naturally handles 2FA, "verify it's you" prompts, etc., since it's really
you doing it). Once you're on the Photos page, come back to this terminal
and press Enter. From then on, amazon_photos_browser_upload.py reuses this
same saved browser profile and stays logged in - no cookies to copy, ever.

Setup (once):
    pip install playwright
    playwright install chromium

Usage:
    python login_setup.py
    python login_setup.py --profile-dir ./my_amazon_profile   # custom location
"""
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_PROFILE_DIR = Path.home() / ".amazon_photos_browser_profile"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    args = p.parse_args()
    args.profile_dir.mkdir(parents=True, exist_ok=True)

    print(f"Saving browser profile to: {args.profile_dir}")
    print("A browser window will open. Log in to Amazon there, navigate to")
    print("Photos, and confirm you can see your library. Then come back here")
    print("and press Enter.\n")

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(args.profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.goto("https://www.amazon.com/photos")
        input("Press Enter here once you're logged in and viewing your Photos library... ")
        context.close()

    print(f"\nDone. Profile saved to {args.profile_dir}")
    print("Run amazon_photos_browser_upload.py whenever you're ready to upload.")


if __name__ == "__main__":
    main()
