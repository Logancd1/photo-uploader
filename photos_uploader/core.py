"""
Upload engine for the Amazon Photos Uploader app. No GUI code here.

Drives the user's own installed Chrome or Edge (no bundled browser) with a
private, persistent profile, so logging in works exactly like normal (2FA etc.)
and the session is remembered between runs.

Dedupe identity is the MD5 of file bytes, tracked in a local SQLite ledger that
is fsync'd after every batch, so a crash or cancel mid-run is safe to resume.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

log = logging.getLogger("apu")

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".webp", ".dng", ".cr2", ".cr3", ".nef", ".arw",
}
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".avi", ".mpg", ".mpeg", ".3gp", ".wmv"}

REGIONS = ["com", "ca", "co.uk", "de", "fr", "it", "es", "co.jp", "com.au", "in", "com.mx", "com.br"]
BROWSER_CHANNELS = ["chrome", "msedge"]  # tried in this order
BATCH_SIZE = 25

# Kept from the original script; centralized so a UI change on Amazon's side is a one-line fix.
SELECTORS = {
    "logged_in_marker": ["[aria-label='Add']", 'text="Add"', "[aria-label='Upload']", 'text="Upload"'],
    "upload_button": [
        "[aria-label='Add']", 'text="Add"', "[aria-label='Upload']", 'text="Upload"',
        "[data-testid='upload-button']",
    ],
    "upload_photos_menu_item": [
        "text=Photos & videos", "text=Photos and videos", "[role='menuitem']:has-text('Photo')",
    ],
    "file_input": ["input[type='file']"],
    "upload_progress": ["[role='progressbar']", "text=Uploading"],
}


class Cancelled(Exception):
    pass


class UploadFailed(Exception):
    """A user-presentable failure (message is shown in the UI)."""


class NotLoggedIn(UploadFailed):
    pass


# --------------------------------------------------------------------------- app data / settings

def app_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / "AmazonPhotosUploader"
    d.mkdir(parents=True, exist_ok=True)
    return d


DEFAULT_SETTINGS = {
    "folders": [], "include_videos": False, "region": "com", "logged_in": False, "channel": None,
}


def load_settings(path: Path | None = None) -> dict:
    path = path or app_dir() / "settings.json"
    try:
        return {**DEFAULT_SETTINGS, **json.loads(path.read_text("utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict, path: Path | None = None):
    path = path or app_dir() / "settings.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2), "utf-8")
    tmp.replace(path)


def photos_url(region: str) -> str:
    return f"https://www.amazon.{region}/photos"


# --------------------------------------------------------------------------- hashing / ledger

def md5_file(path: Path) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def scan(source: Path, exts: set[str]):
    """Yield media files under `source`, skipping hidden files/dirs (incl. macOS ._ files)."""
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if not name.startswith(".") and Path(name).suffix.lower() in exts:
                yield Path(dirpath) / name


class Ledger:
    """Same schema as the original scripts, so existing upload history carries over."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS hashes (
                path TEXT PRIMARY KEY, size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL, md5 TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS uploaded (
                md5 TEXT PRIMARY KEY, path TEXT NOT NULL, node_id TEXT,
                how TEXT NOT NULL, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            """
        )
        self.db.commit()
        self._dirty = 0

    def md5_for(self, path: Path, st: os.stat_result) -> str:
        row = self.db.execute("SELECT size, mtime_ns, md5 FROM hashes WHERE path=?", (str(path),)).fetchone()
        if row and row[0] == st.st_size and row[1] == st.st_mtime_ns:
            return row[2]
        md5 = md5_file(path)
        self.db.execute("INSERT OR REPLACE INTO hashes VALUES (?,?,?,?)", (str(path), st.st_size, st.st_mtime_ns, md5))
        self._dirty += 1
        if self._dirty >= 500:
            self.commit()
        return md5

    def commit(self):
        self.db.commit()
        self._dirty = 0

    def has_uploaded(self, md5: str) -> bool:
        return self.db.execute("SELECT 1 FROM uploaded WHERE md5=?", (md5,)).fetchone() is not None

    def mark_uploaded(self, md5: str, path: Path, how: str = "browser"):
        self.db.execute("INSERT OR REPLACE INTO uploaded (md5, path, node_id, how) VALUES (?,?,?,?)",
                        (md5, str(path), None, how))
        self.commit()

    def close(self):
        self.commit()
        self.db.close()


# --------------------------------------------------------------------------- browser helpers

def _first_match(page: Page, selectors: list[str], timeout: int = 3000):
    last = None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="attached", timeout=timeout)
            return loc
        except PWTimeout as e:
            last = e
    raise last


def launch(pw, settings: dict):
    """Launch the user's installed Chrome/Edge with our private profile. Returns (context, channel)."""
    order = list(BROWSER_CHANNELS)
    if settings.get("channel") in order:  # a profile is tied to the browser that created it
        order = [settings["channel"]]
    errors = []
    for channel in order:
        profile = app_dir() / f"profile-{channel}"
        try:
            ctx = pw.chromium.launch_persistent_context(
                str(profile), channel=channel, headless=False, viewport={"width": 1280, "height": 900},
            )
            return ctx, channel
        except Exception as e:  # not installed, or profile locked by another window
            errors.append(f"{channel}: {str(e).splitlines()[0]}")
    raise UploadFailed(
        "Couldn't open Google Chrome or Microsoft Edge. Install one of them (and close any "
        "leftover uploader browser windows), then try again.\n" + "\n".join(errors)
    )


def _page(ctx):
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def _looks_logged_in(page: Page) -> bool:
    if "/ap/" in page.url:  # Amazon's sign-in / 2FA / captcha flows all live under /ap/
        return False
    try:
        _first_match(page, SELECTORS["logged_in_marker"], timeout=1500)
        return True
    except PWTimeout:
        return False


def _dismiss_overlays(page: Page):
    for _ in range(2):
        page.keyboard.press("Escape")
        time.sleep(0.2)
    close_selectors = ["[aria-label='Close']", "[aria-label='close']", "[aria-label='Dismiss']",
                       "[data-testid='CloseIcon']", "button:has-text('×')"]
    for _ in range(3):
        for sel in close_selectors:
            try:
                buttons = page.locator(sel)
                count = min(buttons.count(), 5)
            except Exception:
                continue
            for i in range(count):
                try:
                    btn = buttons.nth(i)
                    if btn.is_visible():
                        btn.click(timeout=1000)
                        time.sleep(0.2)
                except Exception:
                    continue


def _upload_batch(page: Page, files: list[Path], cancel: threading.Event):
    _dismiss_overlays(page)
    try:
        _first_match(page, SELECTORS["upload_button"]).click()
    except PWTimeout:
        raise UploadFailed("Couldn't find the Add/Upload button on the Amazon Photos page.")
    try:
        _first_match(page, SELECTORS["upload_photos_menu_item"], timeout=5000).click()
    except PWTimeout:
        pass  # some layouts go straight to the file input
    try:
        finput = _first_match(page, SELECTORS["file_input"], timeout=8000)
    except PWTimeout:
        raise UploadFailed("Couldn't find the file chooser after clicking Upload.")
    finput.set_input_files([str(f) for f in files])

    try:
        _first_match(page, SELECTORS["upload_progress"], timeout=10000)
    except PWTimeout:
        pass  # small batches can finish before we look

    # Once handed to the page the batch can't be un-started, so wait it out (cancel applies between batches).
    deadline = time.time() + max(30, 15 * len(files))
    while time.time() < deadline:
        try:
            page.locator(SELECTORS["upload_progress"][0]).first.wait_for(state="hidden", timeout=2000)
            break
        except PWTimeout:
            continue
    else:
        raise UploadFailed(f"Upload of {len(files)} file(s) didn't finish in the expected time.")
    time.sleep(1)


# --------------------------------------------------------------------------- public operations

def sign_in(settings: dict, cancel: threading.Event, timeout: float = 900) -> dict:
    """Open a browser window for the user to log in by hand. Returns updated settings."""
    with sync_playwright() as pw:
        ctx, channel = launch(pw, settings)
        try:
            page = _page(ctx)
            page.goto(photos_url(settings["region"]), wait_until="domcontentloaded")
            log.info("Sign in to Amazon in the window that just opened. It closes on its own when you're done.")
            deadline = time.time() + timeout
            while time.time() < deadline:
                if cancel.is_set():
                    raise Cancelled()
                try:
                    if page.is_closed():
                        break
                    if _looks_logged_in(page):
                        return {**settings, "logged_in": True, "channel": channel}
                    if "/ap/" not in page.url and "/photos" not in page.url:
                        page.goto(photos_url(settings["region"]), wait_until="domcontentloaded")
                except Exception:
                    break  # window closed by the user
                time.sleep(2)
            raise UploadFailed("Sign-in wasn't completed. Click Sign in to try again.")
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def sign_out(settings: dict) -> dict:
    for channel in BROWSER_CHANNELS:
        shutil.rmtree(app_dir() / f"profile-{channel}", ignore_errors=True)
    return {**settings, "logged_in": False, "channel": None}


def upload(
    settings: dict,
    dry_run: bool,
    cancel: threading.Event,
    on_progress: Callable[[int, int], None],
) -> dict:
    """Scan, dedupe, and upload. Returns a summary dict. Raises Cancelled/UploadFailed/NotLoggedIn."""
    sources = [Path(f) for f in settings["folders"]]
    for s in sources:
        if not s.is_dir():
            raise UploadFailed(f"Folder not found: {s}")
    if not sources:
        raise UploadFailed("Add at least one folder first.")
    exts = IMAGE_EXTS | (VIDEO_EXTS if settings["include_videos"] else set())

    ledger = Ledger(app_dir() / "ledger.sqlite3")  # created here: SQLite objects are per-thread
    try:
        pending: list[tuple[Path, str]] = []
        seen: set[str] = set()
        scanned = skipped = 0
        log.info("Scanning folders and checking what's new...")
        for source in sources:
            for path in scan(source, exts):
                if cancel.is_set():
                    raise Cancelled()
                scanned += 1
                if scanned % 200 == 0:
                    log.info("Scanned %d files...", scanned)
                try:
                    st = path.stat()
                    if st.st_size == 0:
                        continue
                    md5 = ledger.md5_for(path, st)
                except OSError as e:
                    log.warning("Unreadable, skipping: %s (%s)", path, e)
                    continue
                if md5 in seen or ledger.has_uploaded(md5):
                    skipped += 1
                    continue
                seen.add(md5)
                pending.append((path, md5))
        ledger.commit()
        log.info("%d file(s) to upload, %d already done.", len(pending), skipped)
        summary = {"to_upload": len(pending), "skipped": skipped, "uploaded": 0}

        if dry_run or not pending:
            for p, _ in pending[:25]:
                log.info("  would upload: %s", p)
            if len(pending) > 25:
                log.info("  ... and %d more", len(pending) - 25)
            return summary

        on_progress(0, len(pending))
        with sync_playwright() as pw:
            ctx, _ = launch(pw, settings)
            try:
                page = _page(ctx)
                page.goto(photos_url(settings["region"]), wait_until="domcontentloaded")
                if not _looks_logged_in(page):
                    raise NotLoggedIn("You're signed out of Amazon. Click Sign in, then try again.")
                for i in range(0, len(pending), BATCH_SIZE):
                    if cancel.is_set():
                        raise Cancelled()
                    batch = pending[i : i + BATCH_SIZE]
                    log.info("Uploading files %d-%d of %d...", i + 1, i + len(batch), len(pending))
                    _upload_batch(page, [p for p, _ in batch], cancel)
                    for p, md5 in batch:
                        ledger.mark_uploaded(md5, p)
                    summary["uploaded"] += len(batch)
                    on_progress(summary["uploaded"], len(pending))
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
        return summary
    finally:
        ledger.close()
