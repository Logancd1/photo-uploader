#!/usr/bin/env python3
"""
Idempotent Amazon Photos uploader that drives a real logged-in browser
instead of calling Amazon's API directly - this exists because the raw-API
approach in amazon_photos_upload.py hit an auth wall (Amazon's session
validation rejects non-browser clients even with every cookie/header copied
over, most likely a TLS/JS fingerprint check). A real browser sidesteps that
by construction.

Every action here is a fixed, specific step (click this exact button, set
these exact files on this exact input) - nothing exploratory or agentic.

SETUP (once)
    pip install playwright
    playwright install chromium
    python login_setup.py              # log in by hand, saves the profile

USAGE
    python amazon_photos_browser_upload.py ~/Pictures/2024 --dry-run
    python amazon_photos_browser_upload.py ~/Pictures/2024

IDENTITY / DEDUPE
    Same as the API version: identity is the file's MD5, tracked in a local
    SQLite ledger (ledger.sqlite3, same schema/file amazon_photos_upload.py
    uses) so a crash mid-run is safe to resume. We can no longer pre-query
    Amazon's full library over the API (that's the auth wall this script
    exists to route around), so the ledger is now the ONLY local dedupe
    check before a file is handed to the browser. Amazon's own upload
    endpoint is still content-addressed server-side, so re-uploading a file
    that's already there should be a harmless no-op rather than a true
    duplicate - but this is inferred, not confirmed against this specific
    web client, so treat the first run on a library with lots of pre-existing
    photos as worth spot-checking afterward.

    Known limitation vs. the API version: this does not mirror your local
    folder structure into Amazon Photos albums - files land in the main
    library, flat. Ask if you want album-mirroring added; it's a fair bit
    more UI automation (open the folder-picker, create-if-missing, etc.)
    and better done as a second pass once the basic upload path is proven
    against the real site.

IF A STEP FAILS
    It'll stop with a message like "Could not find the Upload button" and a
    screenshot saved next to the ledger (debug_<step>.png). Amazon's page
    structure isn't something I can verify without hitting the live site
    from here, so the first run or two may need a quick selector fix -
    send me the screenshot/error and I'll adjust SELECTORS below.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

# Reuse the ledger, scanner, and constants from the API version rather than
# duplicating them - keeps the two scripts' notion of "already uploaded" consistent.
sys.path.insert(0, str(Path(__file__).parent))
from amazon_photos_upload import IMAGE_EXTS, VIDEO_EXTS, Ledger, scan  # noqa: E402

log = logging.getLogger("ap-browser-upload")

DEFAULT_PROFILE_DIR = Path.home() / ".amazon_photos_browser_profile"
DEFAULT_STATE_DIR = Path.home() / ".amazon_photos_uploader"  # shared with the API version
BATCH_SIZE = 25  # files handed to the file input at once

# Centralized so a broken selector is a one-line fix, not a hunt through the script.
# Each is a list tried in order (first match wins) to absorb minor UI variations.
SELECTORS = {
    "logged_in_marker": [
        "[aria-label='Add']",
        'text="Add"',
        "[aria-label='Upload']",
        'text="Upload"',
    ],
    "upload_button": [
        "[aria-label='Add']",
        'text="Add"',            # quoted = exact match, won't catch "Add to album"
        "[aria-label='Upload']",
        'text="Upload"',
        "[data-testid='upload-button']",
    ],
    "upload_photos_menu_item": [
        "text=Photos & videos",
        "text=Photos and videos",
        "[role='menuitem']:has-text('Photo')",
    ],
    "file_input": [
        "input[type='file']",
    ],
    "upload_progress": [
        "[role='progressbar']",
        "text=Uploading",
    ],
    "upload_done_toast": [
        "text=/uploaded|Upload complete|items added/i",
    ],
}


class StepFailed(Exception):
    pass


def first_match(page: Page, selectors: list[str], timeout: int = 3000):
    last_err = None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="attached", timeout=timeout)
            return loc
        except PWTimeout as e:
            last_err = e
    raise last_err


def screenshot(page: Page, state_dir: Path, step: str):
    path = state_dir / f"debug_{step}.png"
    try:
        page.screenshot(path=str(path))
        log.error("Saved a screenshot of the page at this point: %s", path)
    except Exception:
        pass


def ensure_logged_in(page: Page, state_dir: Path, url: str = "https://www.amazon.com/photos"):
    page.goto(url, wait_until="domcontentloaded")
    try:
        first_match(page, SELECTORS["logged_in_marker"], timeout=5000)
    except PWTimeout:
        screenshot(page, state_dir, "not_logged_in")
        raise StepFailed(
            "Doesn't look like we're logged in (no Upload button found on the Photos "
            "page). Run `python login_setup.py` again to refresh the saved session."
        )


def dismiss_overlays(page: Page):
    """Best-effort: close any leftover toast/dialog (e.g. a post-upload 'Add to
    album' popup) that could otherwise block the next click. Escape alone isn't
    reliable against every dialog implementation, so also try clicking any
    visible close (X) control directly."""
    for _ in range(2):
        page.keyboard.press("Escape")
        time.sleep(0.2)
    close_selectors = [
        "[aria-label='Close']", "[aria-label='close']",
        "[aria-label='Dismiss']", "[data-testid='CloseIcon']",
        "button:has-text('×')",
    ]
    for _pass in range(3):  # a second pass catches things only revealed once the top overlay closes
        for sel in close_selectors:
            try:
                buttons = page.locator(sel)
                count = min(buttons.count(), 5)  # cap - there may be more than one (toast + modal)
            except Exception:
                continue
            for i in range(count):
                try:
                    btn = buttons.nth(i)
                    if btn.is_visible():
                        btn.click(timeout=1000)
                        time.sleep(0.2)
                except Exception:
                    continue  # one stuck/covered button shouldn't stop us trying the rest


def upload_batch(page: Page, state_dir: Path, files: list[Path]):
    dismiss_overlays(page)
    try:
        btn = first_match(page, SELECTORS["upload_button"])
        btn.click()
    except PWTimeout:
        screenshot(page, state_dir, "upload_button")
        raise StepFailed("Could not find/click the Add/Upload button.")

    try:
        item = first_match(page, SELECTORS["upload_photos_menu_item"], timeout=5000)
        item.click()
    except PWTimeout:
        pass  # some layouts go straight to a file input with no submenu - fine

    try:
        finput = first_match(page, SELECTORS["file_input"], timeout=8000)
    except PWTimeout:
        screenshot(page, state_dir, "file_input")
        raise StepFailed("Could not find the file input after clicking Upload.")

    finput.set_input_files([str(f) for f in files])  # no OS dialog opens for this

    try:
        first_match(page, SELECTORS["upload_progress"], timeout=10000)
        log.info("Upload in progress for %d file(s)...", len(files))
    except PWTimeout:
        pass  # small/fast batches may finish before we even check

    deadline = time.time() + max(30, 15 * len(files))
    while time.time() < deadline:
        try:
            page.locator(SELECTORS["upload_progress"][0]).first.wait_for(state="hidden", timeout=2000)
            break
        except PWTimeout:
            continue
    else:
        screenshot(page, state_dir, "upload_timeout")
        raise StepFailed(f"Upload of {len(files)} file(s) didn't finish within the expected time.")

    time.sleep(1)  # let any final toast/UI settle before the next batch


def run(sources: list[Path], exts: set[str], ledger: Ledger, page: Page, state_dir: Path, dry_run: bool):
    pending: list[Path] = []
    seen_md5: set[str] = set()
    skipped = 0
    scanned = 0
    for source in sources:
        for path in scan(source, exts):
            scanned += 1
            if scanned % 200 == 0:
                log.info("Scanned %d files so far (hashing to check what's new)...", scanned)
            try:
                st = path.stat()
                if st.st_size == 0:
                    continue
                md5 = ledger.md5_for(path, st)
            except OSError as e:
                log.warning("unreadable, skipping: %s (%s)", path, e)
                continue
            if md5 in seen_md5 or ledger.has_uploaded(md5):
                skipped += 1
                continue
            seen_md5.add(md5)
            pending.append(path)
    ledger.commit()
    log.info("%d file(s) to upload, %d already done.", len(pending), skipped)

    if dry_run:
        for p in pending[:25]:
            log.info("  would upload: %s", p)
        if len(pending) > 25:
            log.info("  ... and %d more", len(pending) - 25)
        return

    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i : i + BATCH_SIZE]
        log.info("Uploading batch %d-%d of %d...", i + 1, i + len(batch), len(pending))
        upload_batch(page, state_dir, batch)
        for f in batch:
            st = f.stat()
            md5 = ledger.md5_for(f, st)
            ledger.mark_uploaded(md5, f, node_id=None, how="browser")
        log.info("Batch done: %d file(s) marked uploaded.", len(batch))


def main() -> int:
    p = argparse.ArgumentParser(description="Browser-driven idempotent Amazon Photos uploader.")
    p.add_argument("folders", nargs="+")
    p.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    p.add_argument("--include-videos", action="store_true")
    p.add_argument("--headless", action="store_true", help="riskier: more likely to be flagged than a visible window")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not args.profile_dir.exists():
        sys.exit(f"No browser profile at {args.profile_dir}. Run `python login_setup.py` first.")

    sources = []
    for f in args.folders:
        d = Path(f).expanduser().resolve()
        if not d.is_dir():
            sys.exit(f"Not a directory: {d}")
        sources.append(d)

    exts = IMAGE_EXTS | (VIDEO_EXTS if args.include_videos else set())
    args.state_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(args.state_dir / "ledger.sqlite3")

    max_attempts = 3
    with sync_playwright() as pw:
        for attempt in range(1, max_attempts + 1):
            log.info("=== Attempt %d/%d ===", attempt, max_attempts)
            context = pw.chromium.launch_persistent_context(
                str(args.profile_dir), headless=args.headless, viewport={"width": 1280, "height": 900}
            )
            try:
                page = context.new_page()
                ensure_logged_in(page, args.state_dir)
                run(sources, exts, ledger, page, args.state_dir, args.dry_run)
                log.info("Done.")
                return 0
            except StepFailed as e:
                log.error("Attempt %d/%d failed: %s", attempt, max_attempts, e)
                if attempt < max_attempts:
                    log.info("Closing the browser and starting over (already-uploaded "
                              "files won't be repeated - the ledger remembers them).")
                else:
                    log.error("Out of retries (%d/%d). Giving up.", max_attempts, max_attempts)
                    return 1
            except KeyboardInterrupt:
                log.warning("Interrupted. Safe to rerun; finished batches are already recorded.")
                return 130
            finally:
                ledger.commit()
                context.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
