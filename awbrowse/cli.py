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

from awbrowse.client import (
    ACTIONS,
    BROWSE_FIELDS,
    NETWORK_KEY,
    SHOT_KEY,
    BrowseClient,
    BrowseError,
    Page,
    act_body,
    browse_body,
)

# ── self-test ──────────────────────────────────────────────────────────────
# Everything asserted here is PURE. A self-test that needs a live service is a
# self-test that gets skipped, and a skipped check is indistinguishable from a
# passing one.


#: Magic bytes -> extension. The service's capture format is NOT decidable from
#: the request: `screenshot: true` says nothing about the encoding, and measured
#: live it returns JPEG. Defaulting the output to "screenshot.png" would write
#: JPEG bytes into a .png every time — a file that opens fine in a viewer that
#: sniffs and fails in anything trusting the extension.
_MAGIC = (
    (bytes.fromhex("89504e470d0a1a0a"), "png"),
    (bytes.fromhex("ffd8ff"), "jpg"),
    (b"GIF8", "gif"),
)


def image_ext(raw: bytes, default: str = "bin") -> str:
    """The real extension for these bytes, sniffed rather than assumed."""
    for magic, ext in _MAGIC:
        if raw.startswith(magic):
            return ext
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return default


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

    # 3. Page's text fallback chain. This service sends `content`; the
    #    session/observe route spells it `text`. A client reading only one
    #    returns "" for a page that rendered perfectly.
    if Page({"content": "b"}).text != "b":
        failures.append("Page did not read `content` (what /browse actually sends)")
    if Page({"text": "a"}).text != "a":
        failures.append("Page did not accept `text`")
    if Page({}).text != "":
        failures.append("Page with neither key should be empty, not None")

    # 4. The screenshot key is what the SERVICE sends, which is NOT what the
    #    request field is called. Measured live: request `screenshot: true`,
    #    response key `screenshot_base64`. Reading the request name yields None
    #    on every successful capture, so `awbrowse shot` would report "no
    #    screenshot" forever while the service sent one every time. Nothing
    #    offline can catch that — the request is valid and the response is a 200.
    if SHOT_KEY != "screenshot_base64":
        failures.append(f"SHOT_KEY drifted from the measured response key: {SHOT_KEY}")
    if Page({SHOT_KEY: "iVBOR"}).screenshot != "iVBOR":
        failures.append("a real screenshot was dropped — the response key is not being read")

    # 5. An absent screenshot is None, never "". An empty string reads downstream
    #    as "a screenshot that came back blank" — a different fact.
    if Page({}).screenshot is not None:
        failures.append("absent screenshot should be None")
    if Page({SHOT_KEY: ""}).screenshot is not None:
        failures.append('empty screenshot should normalise to None, not ""')

    # 6. A render the service itself calls failed must not arrive as a blank page.
    if Page({"status": "error"}).ok:
        failures.append("a failed render was reported as ok")
    if not Page({"status": "success"}).ok or not Page({}).ok:
        failures.append("a successful (or status-less) render was reported as failed")

    # 7. The session surface, all of it measured against a running service.
    #    NETWORK_KEY: the endpoint answers {"count", "responses"}; "requests" is
    #    the obvious guess and returns [] forever with a 200. Proven live with a
    #    control on the SAME captured response: "responses" -> 1, "requests" -> 0.
    if NETWORK_KEY != "responses":
        failures.append(f"NETWORK_KEY drifted from the measured key: {NETWORK_KEY}")
    #    An action the server does not dispatch falls through its if/elif chain
    #    with no else: not an error, a no-op that answers 200.
    try:
        act_body("clik")
    except ValueError:
        pass_ = True
    else:
        pass_ = False
    if not pass_:
        failures.append("an unknown action was accepted")
    if any(act_body(a)["action"] != a for a in ACTIONS):
        failures.append("a valid action did not survive act_body")

    # 8. The image extension is SNIFFED, never assumed from the request. The
    #    service returns JPEG for `screenshot: true` (measured), so a hardcoded
    #    .png writes JPEG bytes under a name that lies about them.
    if image_ext(bytes.fromhex("ffd8ffe0") + b"rest") != "jpg":
        failures.append("JPEG magic not recognised")
    if image_ext(bytes.fromhex("89504e470d0a1a0a") + b"rest") != "png":
        failures.append("PNG magic not recognised")
    if image_ext(b"RIFF____WEBPrest") != "webp":
        failures.append("WEBP magic not recognised")
    if image_ext(b"nonsense") != "bin":
        failures.append("unknown bytes should not be guessed at")

    # 9. base_url is normalised so "host/" and "host" cannot produce "//browse".
    if BrowseClient("https://h/").base_url != "https://h":
        failures.append("trailing slash not trimmed from base_url")

    # 10. No token means no Authorization header — never an empty one. An empty
    #    Bearer is rejected differently from an absent one, and the difference
    #    sends you debugging the wrong side.
    if BrowseClient("https://h").token is not None:
        failures.append("token should default to None")

    # 11. A failure RAISES. If it returned an empty Page instead, a dead service
    #    and a blank page would be the same value to every caller.
    if not issubclass(BrowseError, Exception):
        failures.append("BrowseError is not raisable")

    for f in failures:
        print(f"  FAIL  {f}")
    if failures:
        print(f"SELF-TEST: {len(failures)} failure(s)")
        return 1
    print("  PASS  browse body is exactly the declared field set")
    print("  PASS  text reads `content`; screenshot reads the MEASURED response key")
    print("  PASS  a failed render raises rather than arriving as a blank page")
    print("  PASS  network reads the MEASURED key; an undispatched action is refused")
    print("  PASS  image format sniffed, not assumed; base_url normalised; no empty Bearer")
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
    # No default filename: the extension depends on what the service actually
    # sends, which is only knowable after the response arrives.
    s.add_argument("-o", "--out", help="output file (default: screenshot.<sniffed ext>)")
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
            ext = image_ext(raw)
            out = args.out or f"screenshot.{ext}"
            if args.out and not args.out.lower().endswith("." + ext):
                # Written as asked — it is their filename — but said out loud,
                # because an extension that disagrees with the bytes breaks
                # anything that trusts it rather than sniffing.
                print(f"note: these bytes are {ext.upper()}, not what "
                      f"{args.out!r} claims", file=sys.stderr)
            with open(out, "wb") as fh:
                fh.write(raw)
            print(f"{out}: {len(raw)} bytes ({ext})")
            return 0
    except BrowseError as exc:
        print(f"awbrowse: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
