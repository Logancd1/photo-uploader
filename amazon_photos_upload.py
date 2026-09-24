#!/usr/bin/env python3
"""
Idempotent, crash-safe uploader for Amazon Photos.

HOW IT DECIDES WHAT'S ALREADY UPLOADED
  Identity = MD5 of the file's bytes (the same hash Amazon stores for every
  node), never the filename or path. Before uploading, each file is checked
  against three layers:
    1. Local ledger (SQLite, fsync'd after every upload) - survives crashes
       and power loss, and covers Amazon's search index lagging behind.
    2. A fresh query of every MD5 currently in your Amazon Photos library.
    3. Amazon's own server-side 409 Conflict on duplicate content.
  A crash at any point (mid-upload, or after upload but before the ledger
  write) is safe: the next run finds the file via layer 2 or 3 and skips it.

SETUP
  pip install amazon-photos          # Python 3.11+
  Amazon has no public consumer API, so this uses your logged-in browser
  session cookies (via the unofficial `amazon-photos` library). Log in at
  https://www.amazon.com/photos, then copy these cookies from your browser's
  dev tools (Application/Storage -> Cookies):
      session-id, ubid_main, at_main             (amazon.com)
      session-id, ubid-acb<tld>, at-acb<tld>     (other regions, e.g. "ca")
  and export them (never hardcode them or commit them):
      export AMAZON_SESSION_ID=...  AMAZON_UBID=...  AMAZON_AT=...
      export AMAZON_TLD=com          # default; "ca", "de", "co.uk", etc.
  Cookies expire. If the script reports an auth error, grab fresh ones and
  rerun - the script is idempotent, so it just carries on.

USAGE
  python amazon_photos_upload.py ~/Pictures/2024 /mnt/nas/camera --dry-run
  python amazon_photos_upload.py ~/Pictures/2024 /mnt/nas/camera
  python amazon_photos_upload.py --config uploader.toml

  uploader.toml (optional; CLI flags override):
      folders = ["/home/me/Pictures/2024", "/mnt/nas/camera"]
      include_videos = false
      flat = false
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import random
import sqlite3
import sys
import time
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("ap-upload")

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".webp", ".dng", ".cr2", ".cr3", ".nef", ".arw",
}
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".avi", ".mpg", ".mpeg", ".3gp", ".wmv"}

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 6
TIMEOUT = httpx.Timeout(connect=30, read=300, write=300, pool=30)
DEFAULT_STATE_DIR = Path.home() / ".amazon_photos_uploader"


class AuthError(Exception):
    """Cookies missing/expired. Abort the run; rerun after refreshing them."""


class UploadError(Exception):
    """Non-auth failure for one request. The file stays un-ledgered and is retried next run."""


# --------------------------------------------------------------------------- hashing / scanning

def md5_file(path: Path) -> str:
    h = hashlib.md5(usedforsecurity=False)  # identity check, not security (also OK on FIPS hosts)
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


# --------------------------------------------------------------------------- local ledger

class Ledger:
    """
    `hashes`:   cache of (path, size, mtime) -> md5 so restarts don't rehash the world.
                Losing it is harmless; it's rebuilt.
    `uploaded`: md5s this script has uploaded (or confirmed identical on Amazon).
                Written and fsync'd right after each successful upload.
    """

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
        row = self.db.execute(
            "SELECT size, mtime_ns, md5 FROM hashes WHERE path=?", (str(path),)
        ).fetchone()
        if row and row[0] == st.st_size and row[1] == st.st_mtime_ns:
            return row[2]
        md5 = md5_file(path)
        self.db.execute(
            "INSERT OR REPLACE INTO hashes VALUES (?,?,?,?)",
            (str(path), st.st_size, st.st_mtime_ns, md5),
        )
        self._dirty += 1
        if self._dirty >= 500:  # batch: cache loss on crash is harmless
            self.commit()
        return md5

    def commit(self):
        self.db.commit()
        self._dirty = 0

    def has_uploaded(self, md5: str) -> bool:
        return self.db.execute("SELECT 1 FROM uploaded WHERE md5=?", (md5,)).fetchone() is not None

    def mark_uploaded(self, md5: str, path: Path, node_id: str | None, how: str):
        self.db.execute(
            "INSERT OR REPLACE INTO uploaded (md5, path, node_id, how) VALUES (?,?,?,?)",
            (md5, str(path), node_id, how),
        )
        self.commit()  # durable before we move on


# --------------------------------------------------------------------------- Amazon side

@dataclass
class Result:
    status: str  # "uploaded" | "duplicate" | "conflict"
    node_id: str | None = None
    remote_md5: str | None = None


class Remote:
    """Thin wrapper over the unofficial `amazon-photos` library's authenticated client."""

    def __init__(self, cookies: dict, state_dir: Path, flat: bool = False):
        from amazon_photos import AmazonPhotos  # imported here so tests can stub Remote

        self.flat = flat
        self._folder_ids: dict[tuple[str, ...], str] = {}
        try:
            self.ap = AmazonPhotos(cookies=cookies, db_path=state_dir / "ap_cache.parquet")
        except Exception as e:  # library surfaces bad cookies as assorted JSON/Key/HTTP errors
            raise AuthError(
                f"Could not log in to Amazon Photos ({e!r}). Your cookies are probably "
                "expired or the wrong region (AMAZON_TLD) - copy fresh ones and rerun."
            ) from e
        self.client = self.ap.client
        self.root_id = self.ap.root["id"]

    # -- http helper: retries, backoff, auth detection
    def _send(self, method: str, url: str, *, content=None, accept=(), **kw) -> httpx.Response:
        err = ""
        for attempt in range(MAX_ATTEMPTS):
            try:
                r = self.client.request(
                    method, url, content=content() if content else None, timeout=TIMEOUT, **kw
                )
            except httpx.TransportError as e:
                err = repr(e)
            else:
                if r.status_code in (401, 403):
                    raise AuthError(f"{r.status_code} from Amazon - cookies expired. Refresh and rerun.")
                if r.status_code < 300 or r.status_code in accept:
                    return r
                if r.status_code not in RETRY_STATUS:
                    raise UploadError(f"{method} -> {r.status_code} {r.text[:200]}")
                err = f"HTTP {r.status_code}"
            if attempt == MAX_ATTEMPTS - 1:
                raise UploadError(f"gave up after {MAX_ATTEMPTS} attempts: {err}")
            delay = min(60, 2**attempt) + random.random()
            log.warning("transient error (%s); retrying in %.0fs", err, delay)
            time.sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _json(r: httpx.Response) -> dict:
        try:
            return r.json()
        except ValueError as e:  # e.g. an HTML sign-in page after a silent redirect
            raise AuthError("Amazon returned a non-JSON response - cookies likely expired.") from e

    # -- what's already there
    def existing_md5s(self) -> set[str]:
        # Always a fresh full query; the library's cached parquet is only patched for
        # "today" and can be stale, which is exactly what we can't trust after a crash.
        df = self.ap.query("type:(PHOTOS OR VIDEOS)")
        if df is None or len(df) == 0:
            return set()
        col = next((c for c in ("md5", "contentProperties.md5") if c in df.columns), None)
        if col is None:
            raise UploadError(
                "Amazon's search results had no md5 column, so I can't tell what's already "
                "uploaded. Refusing to continue (the API is unofficial and may have changed)."
            )
        return {str(m).lower() for m in df[col].dropna()}

    # -- folders (mirrors your local layout under the Photos root, like the web uploader)
    def parent_for(self, source: Path, file: Path) -> str:
        if self.flat:
            return self.root_id
        parts = (source.name, *file.parent.relative_to(source).parts)
        parent = self.root_id
        for i in range(len(parts)):
            key = parts[: i + 1]
            if key not in self._folder_ids:
                self._folder_ids[key] = self._ensure_folder(parts[i], parent)
            parent = self._folder_ids[key]
        return parent

    def _ensure_folder(self, name: str, parent_id: str) -> str:
        body = self.ap.base_params | {"kind": "FOLDER", "name": name, "parents": [parent_id]}
        r = self._send("POST", f"{self.ap.drive_url}/nodes", json=body, accept=(409,))
        data = self._json(r)
        fid = data.get("id") or (data.get("info") or {}).get("nodeId")  # 409 => already exists
        if not fid:
            raise UploadError(f"could not create/find folder {name!r}: {str(data)[:200]}")
        return fid

    def _node_md5(self, node_id: str | None) -> str | None:
        if not node_id:
            return None
        try:
            r = self._send("GET", f"{self.ap.drive_url}/nodes/{node_id}", params=self.ap.base_params)
            md5 = (self._json(r).get("contentProperties") or {}).get("md5")
            return md5.lower() if md5 else None
        except UploadError:
            return None

    # -- upload
    def upload(self, path: Path, parent_id: str, md5: str) -> Result:
        def body():
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    yield chunk

        r = self._send(
            "POST", self.ap.cdproxy_url, content=body, accept=(409,),
            params={"name": path.name, "kind": "FILE", "parentNodeId": parent_id},
        )
        data = self._json(r)
        if r.status_code == 409:
            # Either identical content already exists, or the same *name* exists in that
            # folder with different content. Only the former counts as "already uploaded".
            node_id = (data.get("info") or {}).get("nodeId")
            remote_md5 = self._node_md5(node_id)
            return Result("duplicate" if remote_md5 == md5 else "conflict", node_id, remote_md5)
        remote_md5 = (data.get("contentProperties") or {}).get("md5")
        return Result("uploaded", data.get("id"), remote_md5.lower() if remote_md5 else None)


# --------------------------------------------------------------------------- orchestration

@dataclass
class Job:
    source: Path
    path: Path
    md5: str


def plan(sources, exts, ledger: Ledger, remote_md5s: set[str]):
    counts: Counter = Counter()
    jobs: list[Job] = []
    seen: set[str] = set()
    for source in sources:
        for i, path in enumerate(scan(source, exts), 1):
            try:
                st = path.stat()
                if st.st_size == 0:
                    counts["skipped_empty"] += 1
                    continue
                md5 = ledger.md5_for(path, st)
            except OSError as e:
                log.warning("unreadable, skipping: %s (%s)", path, e)
                counts["unreadable"] += 1
                continue
            if md5 in seen:
                counts["skipped_same_content_elsewhere_locally"] += 1
            else:
                seen.add(md5)
                if ledger.has_uploaded(md5):
                    counts["skipped_in_ledger"] += 1
                elif md5 in remote_md5s:
                    counts["skipped_already_on_amazon"] += 1
                else:
                    jobs.append(Job(source, path, md5))
            if i % 1000 == 0:
                log.info("scanned %d files in %s ...", i, source)
    ledger.commit()
    return jobs, counts


def run(sources, exts, ledger: Ledger, remote, dry_run: bool = False) -> Counter:
    log.info("Fetching MD5s of everything already in Amazon Photos ...")
    remote_md5s = remote.existing_md5s()
    log.info("Amazon has %d items. Scanning local folders ...", len(remote_md5s))
    jobs, counts = plan(sources, exts, ledger, remote_md5s)
    log.info("%d file(s) need uploading.", len(jobs))

    if dry_run:
        for j in jobs[:25]:
            log.info("  would upload: %s", j.path)
        if len(jobs) > 25:
            log.info("  ... and %d more", len(jobs) - 25)
        counts["would_upload"] = len(jobs)
        return counts

    for n, job in enumerate(jobs, 1):
        try:
            parent = remote.parent_for(job.source, job.path)
            res = remote.upload(job.path, parent, job.md5)
        except AuthError:
            raise
        except (UploadError, OSError) as e:
            log.error("[%d/%d] FAILED %s: %s", n, len(jobs), job.path, e)
            counts["failed"] += 1
            continue

        if res.status == "conflict":
            log.warning("[%d/%d] CONFLICT %s: name exists on Amazon with different content "
                        "(node %s). Not marked uploaded.", n, len(jobs), job.path, res.node_id)
            counts["conflict"] += 1
            continue
        md5 = job.md5
        if res.remote_md5 and res.remote_md5 != job.md5:
            log.warning("%s changed while uploading; Amazon has %s. Will re-evaluate next run.",
                        job.path, res.remote_md5)
            md5 = res.remote_md5
            counts["changed_during_upload"] += 1
        ledger.mark_uploaded(md5, job.path, res.node_id, res.status)
        counts[res.status] += 1
        log.info("[%d/%d] %s %s", n, len(jobs), res.status, job.path)
    return counts


# --------------------------------------------------------------------------- CLI

def cookies_from_env(tld: str) -> dict:
    try:
        session, ubid, at = (os.environ[k] for k in ("AMAZON_SESSION_ID", "AMAZON_UBID", "AMAZON_AT"))
    except KeyError as e:
        sys.exit(f"Missing environment variable {e}. See the SETUP section at the top of this script.")
    if tld == "com":
        return {"session-id": session, "ubid_main": ubid, "at_main": at}
    return {"session-id": session, f"ubid-acb{tld}": ubid, f"at-acb{tld}": at}


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Idempotent Amazon Photos uploader.")
    p.add_argument("folders", nargs="*", help="local folders to upload (recursive)")
    p.add_argument("--config", type=Path, help="TOML file with folders/include_videos/flat/tld/state_dir")
    p.add_argument("--tld", default=os.environ.get("AMAZON_TLD", "com"), help="amazon.<tld> (default: com)")
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="ledger location")
    p.add_argument("--include-videos", action="store_true", help="also upload video files")
    p.add_argument("--flat", action="store_true", help="put everything in the Photos root, no folders")
    p.add_argument("--dry-run", action="store_true", help="show what would upload; change nothing")
    p.add_argument("-v", "--verbose", action="store_true")
    pre, _ = p.parse_known_args(argv)
    if pre.config:
        cfg = tomllib.loads(pre.config.read_text())
        if "state_dir" in cfg:
            cfg["state_dir"] = Path(cfg["state_dir"])
        p.set_defaults(**cfg)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not args.folders:
        sys.exit("No folders given (pass them as arguments or in --config).")
    sources = []
    for f in args.folders:
        d = Path(f).expanduser().resolve()
        if not d.is_dir():
            sys.exit(f"Not a directory: {d}")
        sources.append(d)

    exts = IMAGE_EXTS | (VIDEO_EXTS if args.include_videos else set())
    ledger = Ledger(args.state_dir / "ledger.sqlite3")
    try:
        remote = Remote(cookies_from_env(args.tld), args.state_dir, flat=args.flat)
        counts = run(sources, exts, ledger, remote, dry_run=args.dry_run)
    except AuthError as e:
        log.error("%s", e)
        return 2
    except UploadError as e:
        log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        log.warning("Interrupted. Safe to rerun; finished uploads are already recorded.")
        return 130
    finally:
        ledger.commit()

    log.info("Done: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to do")
    return 1 if counts["failed"] or counts["conflict"] else 0


if __name__ == "__main__":
    sys.exit(main())
