#!/usr/bin/env python3
"""
Parse a full cookie header from a Chrome dev tools export - handles
"Copy as cURL (bash)", "Copy as cURL (cmd)", and "Copy as fetch" formats -
and use ALL cookies to test Amazon auth.

Usage:
  python cookies_from_curl.py cookies_raw.txt          # parse + live test
  python cookies_from_curl.py cookies_raw.txt --diag   # just show what was
                                                        # found, no network call,
                                                        # no values printed
"""
import re
import sys

import httpx

TLD = "com"


def find_header_lines(text: str) -> list[str]:
    """Return every line/segment that looks like it names a header, for diagnostics."""
    candidates = []
    for m in re.finditer(r"""-H\s+(['"])(.*?)\1""", text, re.DOTALL):
        candidates.append(m.group(2))
    for m in re.finditer(r"""['"]([\w-]+)['"]\s*:\s*['"](.*?)['"]""", text, re.DOTALL):
        candidates.append(f"{m.group(1)}: {m.group(2)}")
    return candidates


def extract_cookie_header(text: str) -> str | None:
    # bash/cmd curl: -H 'cookie: a=1; b=2'   or  -H "cookie: a=1; b=2"  (possibly ^ line-wrapped on Windows)
    cleaned = text.replace("^\r\n", "").replace("^\n", "").replace("\\\r\n", "").replace("\\\n", "")
    m = re.search(r"""-H\s+(['"])\s*cookie\s*:\s*(.*?)\1""", cleaned, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(2)
    # curl --cookie 'a=1; b=2'
    m = re.search(r"""--cookie\s+(['"])(.*?)\1""", cleaned, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(2)
    # "Copy as fetch": JS object with "cookie": "a=1; b=2"
    m = re.search(r"""["']cookie["']\s*:\s*["'](.*?)["']""", cleaned, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1)
    # raw value(s) pasted straight from the dev tools "Request Headers" panel, one
    # header per line. A line like "some-header: value" (that isn't "cookie:") is a
    # *different* header and gets excluded; everything else is treated as part of the
    # cookie value and stitched back together (handles a long value that wrapped
    # across lines when copied, with no real newline in the original).
    header_line = re.compile(r"^([A-Za-z][\w-]*)\s*:\s*(.*)$")
    cookie_parts = []
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        m = header_line.match(line)
        if m and m.group(1).lower() != "cookie":
            continue  # a different header entirely (e.g. x-amz-clouddrive-appid) - skip it
        if m and m.group(1).lower() == "cookie":
            line = m.group(2)
        cookie_parts.append(line)
    blob = "".join(cookie_parts)
    pairs = [p for p in blob.split(";") if "=" in p]
    if len(pairs) >= 2:  # heuristic: a real cookie header has many pairs, not just noise
        return blob
    return None


def extract_extra_header(text: str, name: str) -> str | None:
    """Pull a plain 'name: value' line out of the file, if present."""
    m = re.search(rf"""^\s*{re.escape(name)}\s*:\s*(.+?)\s*$""", text, re.IGNORECASE | re.MULTILINE)
    if m:
        return m.group(1).strip().strip("'\",")
    # also handle it showing up inside a curl -H '...' the same way cookie does
    m = re.search(rf"""-H\s+(['"])\s*{re.escape(name)}\s*:\s*(.*?)\1""", text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(2).strip()
    return None


def parse_cookies(cookie_header: str) -> dict:
    # Dev tools sometimes visually wraps long values (e.g. session-token) and a
    # copy-paste grabs the line break along with it. A real cookie header is one
    # continuous line, so any \r/\n inside it is a paste artifact - strip it.
    cookie_header = cookie_header.replace("\r", "").replace("\n", "")
    cookies = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        cookies[k.strip()] = v.strip()
    return cookies


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python cookies_from_curl.py path/to/cookies_raw.txt [--diag]")
    path = sys.argv[1]
    diag_only = "--diag" in sys.argv[2:]

    text = open(path, "r", encoding="utf-8", errors="replace").read()
    print(f"File is {len(text)} characters, {text.count(chr(10)) + 1} line(s).")

    header = extract_cookie_header(text)
    if not header:
        print("\nNo cookie header matched. Diagnostic - header-like segments found in the file "
              "(names only where recognizable, this does not print your cookie values):")
        found_any = False
        for h in find_header_lines(text):
            name = h.split(":", 1)[0].strip().strip("'\"")
            print("  -", name)
            found_any = True
        if not found_any:
            print("  (none found - this file may not contain a curl/fetch export at all. "
                  "Open it and confirm the first line starts with 'curl ' or 'fetch(')")
        print("\nPaste me the FIRST LINE ONLY of the file (just 'curl ...' or 'fetch(...' opening, "
              "no need for the rest) and I can tell you which format this is.")
        return

    cookies = parse_cookies(header)
    print(f"\nFound {len(cookies)} cookies:")
    for k in cookies:
        print(" -", k)

    appid = extract_extra_header(text, "x-amz-clouddrive-appid")
    if appid:
        print(f"Found x-amz-clouddrive-appid header ({len(appid)} chars) - will include it.")
    else:
        print("No x-amz-clouddrive-appid header found in the file (that's fine if you haven't added it).")

    required = {"session-id", "ubid-main", "at-main"}
    missing = required - cookies.keys()
    if missing:
        print(f"\nWarning: missing expected cookies: {missing}")

    if diag_only:
        return

    url = f"https://www.amazon.{TLD}/drive/v1/nodes"
    params = {"filters": "isRoot:true", "asset": "ALL", "tempLink": "false",
              "resourceVersion": "V2", "ContentType": "JSON"}
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "x-amzn-sessionid": cookies.get("session-id", ""),
    }
    if appid:
        headers["x-amz-clouddrive-appid"] = appid

    print(f"\n--- sending GET {url} with all {len(cookies)} cookies ---")
    try:
        r = httpx.get(url, params=params, headers=headers, cookies=cookies, timeout=30)
    except httpx.HTTPError as e:
        sys.exit(f"Request failed before reaching Amazon: {e}\n"
                  "If this says 'Illegal header value', one of the cookie values still has a "
                  "stray character in it (likely from a wrapped copy-paste) - try re-copying "
                  "the cookie header from dev tools without letting the selection cross a "
                  "visual line-wrap, or copy each long value (x-main, session-token, at-main, "
                  "sess-at-main) individually and rebuild the file by hand.")
    print("status:", r.status_code)
    print("body:  ", r.text[:500])

    if r.status_code == 200:
        print("\nSUCCESS with the full cookie set. This confirms extra cookies were required.")
    else:
        print("\nStill failing even with every cookie the browser sends. This points to the "
              "unofficial API itself being blocked, not a missing cookie.")


if __name__ == "__main__":
    main()
