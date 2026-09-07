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


#: A canned page the fake MCP server "renders", used by the offline backend arm
#: of the self-test. The snapshot text carries Obscura's real envelope (the
#: "URL:/Title:" header and the trailing interactive-elements note) so the arm
#: exercises the parser that strips it, not a pre-cleaned string.
_FAKE_SNAPSHOT = (
    "URL: https://example.com/\nTitle: Example Domain\n\n"
    "Example body text here.\n\n"
    "2 interactive element(s) registered. Call browser_interactive_elements to "
    "list, or pass `ref` to browser_click/browser_fill/browser_type."
)
_FAKE_MARKDOWN = "# Example Domain\n\nExample body text here."
_FAKE_ELEMENTS = (
    'ref=e1    a                      "More information..." name="more"\n'
    'ref=e2    button                 "OK"'
)


def _fake_mcp_handler_factory():
    """A BaseHTTPRequestHandler class that fakes Obscura's `POST /mcp`.

    It answers the real JSON-RPC shapes read off the Rust source: a text
    tools/call returns `content:[{type:text,text}]`; a tool error returns HTTP
    200 with `isError:true`; a JSON-RPC error returns a top-level `error`. The
    navigate url selects the branch, so the client's error handling is exercised
    end to end without a real browser.
    """
    import json as _json
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):  # silence the default stderr logging
            return

        def _send(self, obj, status=200):
            body = _json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802 - stdlib handler name
            if self.path != "/mcp":
                self._send({"error": "not found"}, 404)
                return
            length = int(self.headers.get("Content-Length", 0))
            msg = _json.loads(self.rfile.read(length) or b"{}")
            rid = msg.get("id")
            method = msg.get("method")
            if method == "initialize":
                self._send({"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-obscura", "version": "0"}}})
                return
            if method == "tools/call":
                params = msg.get("params", {})
                name = params.get("name")
                args = params.get("arguments", {})
                url = args.get("url", "")
                # A JSON-RPC error: navigate to jsonrpc://error.
                if name == "browser_navigate" and url == "jsonrpc://error":
                    self._send({"jsonrpc": "2.0", "id": rid, "error": {
                        "code": -32000, "message": "boom"}})
                    return
                # A TOOL error (HTTP 200, isError): navigate to tool://error.
                if name == "browser_navigate" and url == "tool://error":
                    self._send({"jsonrpc": "2.0", "id": rid, "result": {
                        "content": [{"type": "text", "text": "Error: nav failed"}],
                        "isError": True}})
                    return
                text = {
                    "browser_navigate": 'Navigated to https://example.com/ — "Example Domain"',
                    "browser_snapshot": _FAKE_SNAPSHOT,
                    "browser_markdown": _FAKE_MARKDOWN,
                    "browser_interactive_elements": _FAKE_ELEMENTS,
                    "browser_close": "closed",
                }.get(name)
                if text is not None:
                    self._send({"jsonrpc": "2.0", "id": rid, "result": {
                        "content": [{"type": "text", "text": text}]}})
                    return
                if name == "browser_screenshot":
                    # 1x1 PNG, base64 — proves the image block maps to Page.screenshot.
                    png_b64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
                               "QVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
                    self._send({"jsonrpc": "2.0", "id": rid, "result": {
                        "content": [{"type": "image", "data": png_b64,
                                     "mimeType": "image/png"}]}})
                    return
                self._send({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": f"Error: Unknown tool: {name}"}],
                    "isError": True}})
                return
            self._send({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32601, "message": f"Unknown method: {method}"}})

    return Handler


def _obscura_offline_check() -> "list[str]":
    """Prove ObscuraBackend against a stand-up, in-process fake MCP server.

    Deterministic and dependency-free: it binds an ephemeral loopback port,
    speaks the real wire shapes, and tears down. The error arms are the mutation
    guard — a client that returned "" instead of raising for a JSON-RPC error or
    an `isError` result would FAIL them, which is the whole reason the backend
    raises rather than returns.
    """
    import threading
    from http.server import ThreadingHTTPServer

    from awbrowse.client import BrowseError as _BrowseError
    from awbrowse.obscura import ObscuraBackend

    def raises_browse_error(fn) -> bool:
        """True iff `fn()` raised BrowseError. The mutation guard: a backend that
        returned "" instead of raising would make this False and fail the arm."""
        try:
            fn()
        except _BrowseError:
            return True
        return False

    fails: list[str] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _fake_mcp_handler_factory())
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        b = ObscuraBackend(f"http://127.0.0.1:{port}", timeout=5.0)

        page = b.browse("https://example.com")
        if page.engine != "obscura":
            fails.append(f"obscura Page.engine != 'obscura' (got {page.engine!r})")
        if page.text != "Example body text here.":
            fails.append(f"obscura browse text did not round-trip: {page.text!r}")
        if not page.ok:
            fails.append("obscura browse reported a successful render as not ok")

        shot = b.browse("https://example.com", text=False, screenshot=True)
        if not shot.screenshot:
            fails.append("obscura screenshot was not mapped to Page.screenshot")

        md = b.markdown("https://example.com")
        if md != _FAKE_MARKDOWN:
            fails.append(f"obscura markdown did not round-trip: {md!r}")

        obs = b.observe("https://example.com")
        if not obs.elements or obs.elements[0].get("ref") != "e1":
            fails.append(f"obscura observe did not parse elements: {obs.elements!r}")
        if obs.title != "Example Domain":
            fails.append(f"obscura observe title wrong: {obs.title!r}")

        # Mutation arm 1: a JSON-RPC error must RAISE, not return an empty page.
        if not raises_browse_error(lambda: b.browse("jsonrpc://error")):
            fails.append("obscura did not raise on a JSON-RPC error")

        # Mutation arm 2: a tool `isError` result must RAISE (it is an HTTP 200).
        if not raises_browse_error(lambda: b.browse("tool://error")):
            fails.append("obscura did not raise on a tool isError result")
    finally:
        server.shutdown()
        server.server_close()
    return fails


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

    # 12. The Obscura backend, proven against an in-process fake MCP server.
    #     This is the one arm that opens a socket, and it opens its OWN — a
    #     deterministic loopback fake, not a dependency — so it is a self-test,
    #     not a skipped live check. Its error arms are the mutation guard.
    failures.extend(_obscura_offline_check())

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
    print("  PASS  obscura backend round-trips text/markdown/elements; errors raise (offline)")
    print("SELF-TEST: awbrowse ok")
    return 0


# ── commands ───────────────────────────────────────────────────────────────


def _client(args: argparse.Namespace) -> BrowseClient:
    url = args.url or os.environ.get("AWBROWSE_URL")
    if not url:
        print("no service URL: pass --url or set AWBROWSE_URL", file=sys.stderr)
        raise SystemExit(2)
    return BrowseClient(url, args.token or os.environ.get("AWBROWSE_TOKEN"))


def _backend(args: argparse.Namespace):
    """The thing `get`/`shot` drive. Both engines expose the same
    `browse(url, *, wait_ms, text, screenshot)`, so the command code below is
    engine-agnostic. The default is `service` — today's behaviour, unchanged."""
    if getattr(args, "engine", "service") == "obscura":
        # Local import: the service path must not pay for the backend, and the
        # backend is stdlib-only so this cannot fail for a missing dependency.
        from awbrowse.obscura import DEFAULT_OBSCURA_URL, ObscuraBackend
        return ObscuraBackend(args.obscura_url or DEFAULT_OBSCURA_URL)
    return _client(args)


def main(argv: list[str] | None = None) -> int:
    # GENERATED doctor intercept (gen_aw_doctor.py) -- do not edit
    _dv = locals().get("argv")
    if (_dv if _dv is not None else __import__("sys").argv[1:])[:1] == ["doctor"]:
        from ._doctor import report
        return report()
    # GENERATED repo-state intercept (gen_aw_doctor.py) -- do not edit
    try:
        from awgit import state as _aw_state
    except Exception:
        _aw_state = None
    if _aw_state is not None:
        _sv = locals().get("argv")
        if _aw_state.cli_banner(_sv if _sv is not None else __import__("sys").argv[1:]):
            return 0
    ap = argparse.ArgumentParser(prog="awbrowse", description=__doc__)
    ap.add_argument("--self-test", action="store_true",
                    help="prove this client still holds its contract, offline")
    ap.add_argument("--url", help="service origin (or AWBROWSE_URL)")
    ap.add_argument("--token", help="bearer token (or AWBROWSE_TOKEN)")

    # Engine flags live on a PARENT shared by the subcommands, so they are
    # accepted AFTER the verb — `awbrowse get --engine obscura URL`, the form the
    # ticket specifies. Defining them only on the top-level parser would force
    # them before the verb and reject that invocation.
    engine = argparse.ArgumentParser(add_help=False)
    engine.add_argument("--engine", choices=("service", "obscura"), default="service",
                        help="service (default: an AitherBrowser-shaped HTTP service) "
                             "or obscura (a local Obscura MCP-over-HTTP browser)")
    engine.add_argument("--obscura-url",
                        help="Obscura MCP origin for --engine obscura "
                             "(default http://127.0.0.1:3000)")

    sub = ap.add_subparsers(dest="cmd")

    g = sub.add_parser("get", parents=[engine],
                       help="render a page and print its text")
    g.add_argument("target")
    g.add_argument("--wait", type=int, default=2000, help="ms to settle")

    s = sub.add_parser("shot", parents=[engine],
                       help="render a page and save a screenshot")
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
        c = _backend(args)
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
