#!/usr/bin/env python3
"""
Standalone auth debug for Amazon Photos - no amazon-photos library involved,
so we can see exactly what's sent and what comes back.

Run it the same way you ran the uploader (same terminal, cookies already exported):
    python debug_auth.py
"""
import os
import sys

import httpx

TLD = os.environ.get("AMAZON_TLD", "com")


def mask(v: str) -> str:
    if not v:
        return "(EMPTY)"
    return f"{v[:6]}...{v[-4:]}  (length={len(v)})"


def main():
    session = os.environ.get("AMAZON_SESSION_ID", "")
    ubid = os.environ.get("AMAZON_UBID", "")
    at = os.environ.get("AMAZON_AT", "")

    print("--- values as seen by Python ---")
    print("AMAZON_SESSION_ID:", mask(session))
    print("AMAZON_UBID:      ", mask(ubid))
    print("AMAZON_AT:        ", mask(at))
    for name, v in [("AMAZON_SESSION_ID", session), ("AMAZON_UBID", ubid), ("AMAZON_AT", at)]:
        if v != v.strip():
            print(f"  !! {name} has leading/trailing whitespace - that alone can break auth")
        if v[:1] in "'\"" or v[-1:] in "'\"":
            print(f"  !! {name} appears to still have a quote character in it")
    if not (session and ubid and at):
        sys.exit("\nOne or more variables is empty in THIS terminal. Export them here and rerun.")

    if TLD == "com":
        cookies = {"session-id": session, "ubid_main": ubid, "at_main": at}
    else:
        cookies = {"session-id": session, f"ubid-acb{TLD}": ubid, f"at-acb{TLD}": at}

    print("\n--- cookie keys being sent ---")
    print(list(cookies.keys()))

    url = f"https://www.amazon.{TLD}/drive/v1/nodes"
    params = {"filters": "isRoot:true", "asset": "ALL", "tempLink": "false",
              "resourceVersion": "V2", "ContentType": "JSON"}
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "x-amzn-sessionid": session,
    }

    print(f"\n--- sending GET {url} ---")
    r = httpx.get(url, params=params, headers=headers, cookies=cookies, timeout=30)
    print("status:", r.status_code)
    print("body:  ", r.text[:500])

    if r.status_code == 200:
        print("\nSUCCESS - auth works. The uploader script should work now too.")
    elif r.status_code in (401, 403):
        print("\nStill unauthorized. Likely causes at this point:")
        print("  - cookies copied from the wrong domain (must be www.amazon.<tld>, not photos.amazon.<tld>)")
        print("  - AMAZON_TLD doesn't match the Amazon site you're actually logged into")
        print("  - the account itself isn't logged into Amazon Photos in that browser session")
        print("    (try loading https://www.amazon.com/photos in that same browser first)")


if __name__ == "__main__":
    main()
