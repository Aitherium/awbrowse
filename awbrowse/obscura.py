"""Drive a LOCAL Obscura browser over MCP-over-HTTP.

WHY A BACKEND AND NOT A LIFT
============================
Obscura is a 165k-line Rust browser engine (V8, its own DOM, CPU paint). Lifting
any of it into this package is impossible and pointless: the value is the running
BINARY, not the source. So this is the same move `client.py` made for
AitherBrowser and `awrelay` made for the relay server — a small standalone client
that speaks a documented wire protocol to a server someone else runs. Here the
server is `obscura mcp --http` (the binary awnix ships), and the wire is MCP
JSON-RPC over HTTP.

`ObscuraBackend` maps that wire onto the SAME `Page` / `Observation` dataclasses
`BrowseClient` returns, so a caller written against awbrowse does not learn a new
shape to gain a local, GPU-free browser.

THE WIRE CONTRACT (read off the Obscura v0.2.1 Rust source, not guessed)
=======================================================================
All references are to `crates/obscura-mcp/src/` in h4ckf0r0day/obscura.

* Transport is a SINGLE route, `POST /mcp`, carrying one JSON-RPC 2.0 message
  (`http.rs`: routing rejects any other path with 404). A `Content-Length` header
  is REQUIRED — its absence is answered 400 "missing Content-Length"
  (`http.rs` `RequestBody::MissingLength`). `urllib` sets it for us when `data=`
  is passed.
* A POST answers with `Content-Type: application/json` and a plain JSON body
  (`http.rs` `respond_json`). SSE (`text/event-stream`) is used ONLY for a
  `GET` that asks for it, never for a tool call — but this client still parses an
  SSE `data:` body defensively, so a future streaming version does not silently
  return nothing.
* No authentication is enforced and there is no session id: the `Authorization`
  / `X-API-Key` headers appear only in the CORS preflight allow-list
  (`http.rs` OPTIONS handler), and nothing gates a call on them. So there is no
  token to send and none is invented.
* A browser (a caller sending an `Origin` header) is subject to an origin
  allowlist; a NATIVE client that sends no `Origin` is always allowed
  (`http.rs` `origin_allowed`). This client therefore sends NO `Origin` header.
* `dispatch` (`lib.rs`) routes `initialize`, `tools/list`, `tools/call`. The HTTP
  transport does not require `initialize` first, but this client performs it once
  anyway — it is correct MCP etiquette and cheap. `initialize` returns
  `protocolVersion "2024-11-05"` (`lib.rs` `handle_initialize`).
* A `tools/call` result is `{"content": [{"type": "text", "text": <str>}]}` for a
  text tool (`lib.rs` `handle_tool_call`), or `{"content": [{"type": "image",
  "data": <base64>, "mimeType": "image/png"}]}` for `browser_screenshot`
  (`lib.rs` `tool_screenshot`).

THE TRAP THAT WOULD MAKE A FAILURE LOOK LIKE A BLANK PAGE
=========================================================
A tool that FAILS does not return a JSON-RPC error and does not return a non-200.
It returns an ordinary HTTP 200 `tools/call` result with `"isError": true` and a
`text` block reading `"Error: ..."` (`lib.rs` `handle_tool_call`). A client that
only checked the JSON-RPC envelope would hand that string back as page content —
the exact silence `client.py` refuses. So `_tool_text` raises `BrowseError` on
`isError`, and never returns `""` for a failure.

PER-TOOL OUTPUT (so the mapping onto Page/Observation is not a guess)
====================================================================
* `browser_navigate` → `"Navigated to <url> — \"<title>\""` (`lib.rs` tool_navigate).
* `browser_snapshot` → `"URL: <url>\nTitle: <title>\n\n<body>"` plus a trailing
  "N interactive element(s) registered…" note (`lib.rs` tool_snapshot). This
  client strips that envelope so `Page.text` is the readable body alone.
* `browser_markdown` → the markdown string directly (`lib.rs` tool_markdown).
* `browser_interactive_elements` → newline-joined
  `ref=<r> <tag[type]> "<label>" name="<name>"` lines (`lib.rs`
  tool_interactive_elements), parsed back into dicts for `Observation.elements`.
* `browser_screenshot` → a base64 PNG image block (`lib.rs` tool_screenshot).

NOTE `browser_search` IS NOT WEB SEARCH. Upstream's `browser_search` is an
in-page text FIND over the current document (`lib.rs` tool_search), not a query
to a search engine. It is deliberately not surfaced here, so no caller mistakes
it for AitherSearch.

Stdlib only, on purpose: this backend must work inside an awnix container that
has no fleet and no third-party Python. It uses `urllib`, never `httpx`.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import warnings
from typing import Any, Optional

from awbrowse.client import BrowseError, Observation, Page

__all__ = ["ObscuraBackend", "DEFAULT_OBSCURA_URL"]

#: The default loopback origin an awnix `obscura mcp --http --host 127.0.0.1
#: --port 3000` user service listens on. Loopback because the MCP port drives a
#: real browser and is unauthenticated; it must never be published off the host.
DEFAULT_OBSCURA_URL = "http://127.0.0.1:3000"

#: The protocol version this client announces on initialize. It matches the
#: version the server reports (obscura v0.2.1 `handle_initialize`); the transport
#: does not gate on it, but sending a matching one is correct and future-proofs
#: a server that starts to care.
_MCP_PROTOCOL_VERSION = "2024-11-05"

#: The three navigation wait conditions Obscura's `browser_navigate` accepts
#: (its inputSchema enum). Anything else is refused here rather than sent: the
#: server would fall back to "load" silently, so a typo would change behaviour
#: with no error.
_WAIT_UNTIL = ("load", "domcontentloaded", "networkidle0")

#: A generous default character budget for text/markdown. Obscura truncates both
#: `browser_snapshot` and `browser_markdown` to 4000 chars by default
#: (`DEFAULT_TEXT_LIMIT`), and a silent 4000-char cap on a page read is a wrong
#: answer that looks like a short page. This asks for far more; pass `max_chars`
#: to change it. Never 0 — the server treats 0 as "truncate to nothing".
_DEFAULT_MAX_CHARS = 200_000

#: Parses one `browser_interactive_elements` line back into its parts. The label
#: is Rust-`{:?}`-quoted and an optional `name="..."` may follow. Best-effort:
#: a line that does not match is kept verbatim rather than dropped, because a
#: silently discarded element is one an agent cannot decide to click.
_ELEM_RE = re.compile(
    r'^ref=(?P<ref>\S+)\s+(?P<kind>.+?)\s+"(?P<label>.*?)"'
    r'(?:\s+name="(?P<name>.*)")?\s*$'
)


class ObscuraBackend:
    """An awbrowse backend that drives a local Obscura MCP-over-HTTP server.

    Returns the same `Page` / `Observation` objects as `BrowseClient`, with
    `Page.engine == "obscura"`. Raises `BrowseError` on any transport failure,
    non-200, JSON-RPC error, or tool `isError` — it never returns an empty page
    for a failure, because a dead server and a blank page are different facts.
    """

    def __init__(self, base_url: str = DEFAULT_OBSCURA_URL,
                 timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._rpc_id = 0
        self._initialized = False

    # ── transport ────────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _decode_body(self, content_type: str, raw: bytes) -> dict:
        """Parse a `/mcp` response body: plain JSON, or SSE `data:` lines.

        The live transport answers plain JSON for a POST. SSE is handled anyway
        so a future streaming server does not read as "returned nothing".
        """
        text = raw.decode("utf-8", "replace").strip()
        is_sse = "text/event-stream" in (content_type or "").lower() or (
            text.startswith("data:") or "\ndata:" in text)
        if is_sse:
            payloads = [ln[5:].strip() for ln in text.splitlines()
                        if ln.startswith("data:")]
            # Prefer the last data line that parses to an object carrying our
            # result; fall back to the last that parses at all.
            parsed: Optional[dict] = None
            for chunk in payloads:
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    parsed = obj
            if parsed is None:
                raise BrowseError(
                    f"/mcp: SSE body carried no parseable JSON: {text[:200]!r}")
            return parsed
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BrowseError(f"/mcp: response was not JSON: {exc}: "
                              f"{text[:200]!r}") from exc
        if not isinstance(obj, dict):
            raise BrowseError(f"/mcp: expected a JSON object, got {type(obj).__name__}")
        return obj

    def _rpc(self, method: str, params: dict) -> dict:
        """One JSON-RPC call. Returns the `result` object, raising on any error.

        No `Origin` header: this is a native client, and sending one would opt
        into the server's browser-origin allowlist for no reason.
        """
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/mcp",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                # The server accepts plain JSON for a POST; advertise both so a
                # streaming variant can choose to answer either.
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001 - best effort on the error body
                detail = ""  # the error body was unreadable; the code still carries the fact
            raise BrowseError(f"/mcp {method}: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            # A refused/timed-out connection reads as "the service is down".
            # Surfaced, never swallowed into an empty page.
            raise BrowseError(f"/mcp {method}: {exc.reason}") from exc
        except OSError as exc:
            raise BrowseError(f"/mcp {method}: {exc}") from exc

        obj = self._decode_body(ctype, raw)
        if "error" in obj and obj["error"] is not None:
            err = obj["error"]
            if isinstance(err, dict):
                raise BrowseError(
                    f"/mcp {method}: JSON-RPC error {err.get('code')}: "
                    f"{err.get('message')}")
            raise BrowseError(f"/mcp {method}: JSON-RPC error: {err!r}")
        result = obj.get("result")
        if not isinstance(result, dict):
            raise BrowseError(f"/mcp {method}: no result object in {obj!r}")
        return result

    def _initialize(self) -> None:
        """Perform the MCP handshake once. Idempotent within a backend."""
        if self._initialized:
            return
        self._rpc("initialize", {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "awbrowse", "version": "obscura-backend"},
        })
        self._initialized = True

    # ── tool calls ────────────────────────────────────────────────────────────

    def _call(self, name: str, arguments: dict) -> dict:
        """Raw `tools/call`, returning the result object and RAISING on isError.

        A tool failure is an HTTP 200 with `isError: true` and a `text` block
        that begins `Error:` — so this is the one check that separates a real
        result from a failure dressed as one.
        """
        self._initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            text = self._join_text(result)
            raise BrowseError(f"obscura {name} failed: {text or result!r}")
        return result

    @staticmethod
    def _join_text(result: dict) -> str:
        """Concatenate the text of every text block in a tools/call result."""
        blocks = result.get("content")
        if not isinstance(blocks, list):
            return ""
        parts = [b.get("text", "") for b in blocks
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts)

    @staticmethod
    def _image_block(result: dict) -> Optional[dict]:
        blocks = result.get("content")
        if not isinstance(blocks, list):
            return None
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "image":
                return b
        return None

    def _call_text(self, name: str, arguments: dict) -> str:
        return self._join_text(self._call(name, arguments))

    # ── parsing helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_snapshot(text: str) -> tuple[str, str, str]:
        """Split `browser_snapshot`'s "URL:\\nTitle:\\n\\n<body>" envelope.

        Returns (url, title, body) with the trailing interactive-elements note
        removed, so `Page.text` is the readable body alone.
        """
        url = title = ""
        lines = text.split("\n")
        if lines and lines[0].startswith("URL: "):
            url = lines[0][len("URL: "):].strip()
        if len(lines) > 1 and lines[1].startswith("Title: "):
            title = lines[1][len("Title: "):]
        parts = text.split("\n\n", 1)
        body = parts[1] if len(parts) > 1 else ""
        marker = ("interactive element(s) registered. Call "
                  "browser_interactive_elements")
        idx = body.rfind(marker)
        if idx != -1:
            cut = body.rfind("\n\n", 0, idx)
            if cut != -1:
                body = body[:cut]
        return url, title, body.strip()

    @classmethod
    def _parse_elements(cls, text: str) -> list[dict]:
        """Parse `browser_interactive_elements` text into a list of dicts.

        The raw line is always kept, so a line the regex cannot split is
        surfaced rather than dropped.
        """
        if not text or text.strip() == "No interactive elements on this page.":
            return []
        out: list[dict] = []
        for line in text.splitlines():
            line = line.rstrip()
            if not line:
                continue
            m = _ELEM_RE.match(line)
            if m:
                out.append({
                    "ref": m.group("ref"),
                    "kind": m.group("kind").strip(),
                    "label": m.group("label"),
                    "name": m.group("name") or "",
                    "line": line,
                })
            else:
                out.append({"line": line})
        return out

    @staticmethod
    def _check_wait_until(wait_until: str) -> str:
        if wait_until not in _WAIT_UNTIL:
            raise ValueError(
                f"wait_until must be one of {_WAIT_UNTIL}, got {wait_until!r}")
        return wait_until

    def _navigate(self, url: str, wait_until: str) -> None:
        self._call_text("browser_navigate",
                        {"url": url, "waitUntil": self._check_wait_until(wait_until)})

    # ── public API (mirrors BrowseClient where the shapes fit) ───────────────

    def browse(self, url: str, *, wait_ms: int = 2000, text: bool = True,
               screenshot: bool = False, wait_until: str = "load",
               max_chars: int = _DEFAULT_MAX_CHARS) -> Page:
        """Render one page and return a `Page` with `engine == "obscura"`.

        `wait_ms` is accepted for parity with `BrowseClient.browse`, but Obscura
        waits on a page LIFECYCLE condition (`wait_until`: load /
        domcontentloaded / networkidle0), not a fixed millisecond delay — so the
        millisecond value is advisory here and `wait_until` is the real control.
        This is stated rather than silently ignored.
        """
        self._navigate(url, wait_until)
        page_url = url
        content = ""
        if text:
            snap = self._call_text("browser_snapshot", {"max_chars": max_chars})
            parsed_url, _title, body = self._parse_snapshot(snap)
            page_url = parsed_url or url
            content = body
        raw: dict[str, Any] = {
            "url": page_url,
            "content": content,
            "status": "success",
            "engine": "obscura",
        }
        if screenshot:
            result = self._call("browser_screenshot", {})
            img = self._image_block(result)
            if not img or not img.get("data"):
                raise BrowseError(
                    "obscura browser_screenshot returned no image (the pinned "
                    "build may lack the render feature)")
            # Same shape BrowseClient's Page reads: base64 under the shot key.
            raw["screenshot_base64"] = img["data"]
        return Page(raw)

    def observe(self, url: Optional[str] = None, *, wait_until: str = "load",
                screenshot: bool = False,
                max_chars: int = _DEFAULT_MAX_CHARS) -> Observation:
        """Read the current (or a freshly navigated) page as an `Observation`.

        Carries `elements` — the interactive list an OODA loop decides from —
        which a `Page` does not. If `url` is given the page is navigated first;
        otherwise the server's current page is observed.
        """
        if url is not None:
            self._navigate(url, wait_until)
        snap = self._call_text("browser_snapshot", {"max_chars": max_chars})
        page_url, title, body = self._parse_snapshot(snap)
        elems = self._parse_elements(
            self._call_text("browser_interactive_elements", {}))
        raw: dict[str, Any] = {
            "url": page_url or (url or ""),
            "title": title,
            "text": body,
            "elements": elems,
        }
        if screenshot:
            result = self._call("browser_screenshot", {})
            img = self._image_block(result)
            if not img or not img.get("data"):
                raise BrowseError(
                    "obscura browser_screenshot returned no image (the pinned "
                    "build may lack the render feature)")
            raw["screenshot_base64"] = img["data"]
        return Observation(raw)

    def interactive_elements(self, url: str, *,
                             wait_until: str = "load") -> list[dict]:
        """Navigate and return the page's interactive elements as dicts."""
        self._navigate(url, wait_until)
        return self._parse_elements(
            self._call_text("browser_interactive_elements", {}))

    def markdown(self, url: str, *, wait_until: str = "load",
                 max_chars: int = _DEFAULT_MAX_CHARS) -> str:
        """Navigate and return the page as Markdown (headings, lists, links)."""
        self._navigate(url, wait_until)
        return self._call_text("browser_markdown", {"max_chars": max_chars})

    def close(self) -> None:
        """Close the current browser page on the server (best effort).

        The server holds a real browser page; leaving it open leaks state on a
        long-lived MCP process. Best-effort because raising here would replace
        whatever exception the caller's `with` block was already carrying — but
        NEVER silent: a failed close is warned, matching `Session.__exit__` in
        client.py, so a leak is visible rather than swallowed into "sessions are
        cleaned up".
        """
        try:
            self._call("browser_close", {})
        except BrowseError as exc:
            warnings.warn(f"awbrowse: obscura page was not closed: {exc}",
                          RuntimeWarning, stacklevel=2)
        self._initialized = False

    def __enter__(self) -> "ObscuraBackend":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
