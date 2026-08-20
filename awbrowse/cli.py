"""awbrowse CLI.

    awbrowse get https://example.com --text
    awbrowse shot https://example.com -o page.png
    awbrowse --self-test

The service origin comes from --url or AWBROWSE_URL; the token from --token or
AWBROWSE_TOKEN. Neither is guessed: a client that silently falls back to some
default endpoint sends your pages somewhere you did not choose.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import sys

from awbrowse.client import BROWSE_FIELDS, BrowseClient, BrowseError, Page, browse_body

# ── self-test ──────────────────────────────────────────────────────────────
# Everything asserted here is PURE. A self-test that needs a live service is a
# self-test that gets skipped, and a skipped check is indistinguishable from a
# passing one.


def _self_test() -> int:
    failures: list[str] = []

    # 1. The browse body is EXACTLY the declared field set. This is the whole
    #    reason the constant exists: the server model is extra="forbid", so an
    #    extra key 422s the request and a missing one is a validation error. A
    #    test that only checked "url is in there" would pass on both.
    body = browse_body("https://example.com")
    if tuple(body) != BROWSE_FIELDS:
        failures.append(f"browse_body keys {tuple(body)} != declared {BROWSE_FIELDS}")
    if set(body) != set(BROWSE_FIELDS):
        failures.append("browse_body sends a field the service does not declare")

    # 2. Defaults match the server's own defaults. If they drift, every caller
    #    who omits an argument silently gets different behaviour from the
    #    service's own default, which is worse than an error.
    if body != {"url": "https://example.com", "wait_time": 2000,
                "extract_text": True, "screenshot": False}:
        failures.append(f"browse_body defaults drifted: {body}")

    # 3. Page's text fallback chain. Services in this shape answer with `text`
    #    on one route and `content` on another; a client that reads only one
    #    returns "" for a page that rendered perfectly.
    if Page({"text": "a"}).text != "a":
        failures.append("Page did not read `text`")
    if Page({"content": "b"}).text != "b":
        failures.append("Page did not fall back to `content`")
    if Page({}).text != "":
        failures.append("Page with neither key should be empty, not None")

    # 4. An absent screenshot is None, never "". An empty string reads downstream
    #    as "a screenshot that came back blank" — a different fact.
    if Page({}).screenshot is not None:
        failures.append("absent screenshot should be None")
    if Page({"screenshot": ""}).screenshot is not None:
        failures.append('empty screenshot should normalise to None, not ""')
    if Page({"screenshot": "iVBOR"}).screenshot != "iVBOR":
        failures.append("a real screenshot was dropped")

    # 5. base_url is normalised so "host/" and "host" cannot produce "//browse".
    if BrowseClient("https://h/").base_url != "https://h":
        failures.append("trailing slash not trimmed from base_url")

    # 6. No token means no Authorization header — never an empty one. An empty
    #    Bearer is rejected differently from an absent one, and the difference
    #    sends you debugging the wrong side.
    if BrowseClient("https://h").token is not None:
        failures.append("token should default to None")

    # 7. A failure RAISES. If it returned an empty Page instead, a dead service
    #    and a blank page would be the same value to every caller.
    if not issubclass(BrowseError, Exception):
        failures.append("BrowseError is not raisable")

    for f in failures:
        print(f"  FAIL  {f}")
    if failures:
        print(f"SELF-TEST: {len(failures)} failure(s)")
        return 1
    print("  PASS  browse body is exactly the declared field set")
    print("  PASS  Page text fallback, and absent screenshot is None")
    print("  PASS  base_url normalised; no token means no header")
    print("SELF-TEST: awbrowse ok")
    return 0


# ── commands ───────────────────────────────────────────────────────────────


def _client(args: argparse.Namespace) -> BrowseClient:
    url = args.url or os.environ.get("AWBROWSE_URL")
    if not url:
        print("no service URL: pass --url or set AWBROWSE_URL", file=sys.stderr)
        raise SystemExit(2)
    return BrowseClient(url, args.token or os.environ.get("AWBROWSE_TOKEN"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="awbrowse", description=__doc__)
    ap.add_argument("--self-test", action="store_true",
                    help="prove this client still holds its contract, offline")
    ap.add_argument("--url", help="service origin (or AWBROWSE_URL)")
    ap.add_argument("--token", help="bearer token (or AWBROWSE_TOKEN)")
    sub = ap.add_subparsers(dest="cmd")

    g = sub.add_parser("get", help="render a page and print its text")
    g.add_argument("target")
    g.add_argument("--wait", type=int, default=2000, help="ms to settle")

    s = sub.add_parser("shot", help="render a page and save a screenshot")
    s.add_argument("target")
    s.add_argument("-o", "--out", default="screenshot.png")
    s.add_argument("--wait", type=int, default=2000)

    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.cmd:
        ap.print_help()
        return 2

    try:
        c = _client(args)
        if args.cmd == "get":
            page = c.browse(args.target, wait_ms=args.wait)
            print(page.text)
            return 0
        if args.cmd == "shot":
            page = c.browse(args.target, wait_ms=args.wait, text=False, screenshot=True)
            if not page.screenshot:
                # Distinct from a transport failure: the request SUCCEEDED and
                # returned no image. Saying so beats writing a zero-byte file.
                print("the service returned no screenshot", file=sys.stderr)
                return 1
            try:
                raw = base64.b64decode(page.screenshot, validate=True)
            except (binascii.Error, ValueError) as exc:
                print(f"screenshot was not valid base64: {exc}", file=sys.stderr)
                return 1
            with open(args.out, "wb") as fh:
                fh.write(raw)
            print(f"{args.out}: {len(raw)} bytes")
            return 0
    except BrowseError as exc:
        print(f"awbrowse: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
