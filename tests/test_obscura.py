"""What must stay true of the Obscura MCP-over-HTTP backend.

These overlap the CLI `--self-test`'s offline arm on purpose. The self-test is
what ships (it stands up its own fake server and runs on any install, no pytest);
this is what runs in CI with the mutation guards that prove each assertion can
still fail.

Every wire shape asserted here was read off obscura v0.2.1's Rust source
(`crates/obscura-mcp/src/lib.rs` and `http.rs`), not invented — see the module
docstring of `awbrowse/obscura.py` for the file:line citations.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from awbrowse.client import BrowseError, Observation, Page
from awbrowse.obscura import DEFAULT_OBSCURA_URL, ObscuraBackend

# ── pure parsing (no socket) ─────────────────────────────────────────────────

_SNAPSHOT = (
    "URL: https://example.com/\nTitle: Example Domain\n\n"
    "Example body text here.\n\n"
    "2 interactive element(s) registered. Call browser_interactive_elements to "
    "list, or pass `ref` to browser_click/browser_fill/browser_type."
)


def test_parse_snapshot_strips_the_envelope():
    url, title, body = ObscuraBackend._parse_snapshot(_SNAPSHOT)
    assert url == "https://example.com/"
    assert title == "Example Domain"
    # The URL/Title header AND the trailing interactive-elements note are gone;
    # Page.text must be the readable body alone.
    assert body == "Example body text here."


def test_parse_snapshot_without_refs_note_keeps_body():
    text = "URL: https://x/\nTitle: T\n\nJust the body."
    url, title, body = ObscuraBackend._parse_snapshot(text)
    assert (url, title, body) == ("https://x/", "T", "Just the body.")


def test_parse_elements_reads_ref_and_label():
    text = ('ref=e1    a                      "More information..." name="more"\n'
            'ref=e2    button                 "OK"')
    els = ObscuraBackend._parse_elements(text)
    assert els[0]["ref"] == "e1"
    assert els[0]["label"] == "More information..."
    assert els[0]["name"] == "more"
    assert els[1]["ref"] == "e2"
    assert els[1]["label"] == "OK"


def test_parse_elements_none_message_is_empty_list():
    assert ObscuraBackend._parse_elements("No interactive elements on this page.") == []


def test_parse_elements_keeps_an_unparseable_line_rather_than_dropping_it():
    # A silently dropped element is one an agent cannot decide to click.
    els = ObscuraBackend._parse_elements("garbage line with no ref")
    assert els == [{"line": "garbage line with no ref"}]


def test_wait_until_is_validated_not_silently_replaced():
    # Obscura falls back to "load" for an unknown condition; refusing here means
    # a typo is a loud error, not a quiet behaviour change.
    assert ObscuraBackend._check_wait_until("networkidle0") == "networkidle0"
    with pytest.raises(ValueError):
        ObscuraBackend._check_wait_until("networkidle2")


def test_join_text_concatenates_text_blocks_only():
    result = {"content": [
        {"type": "text", "text": "a"},
        {"type": "image", "data": "x"},
        {"type": "text", "text": "b"},
    ]}
    assert ObscuraBackend._join_text(result) == "ab"


def test_image_block_is_found_among_content():
    result = {"content": [{"type": "text", "text": "x"},
                          {"type": "image", "data": "d", "mimeType": "image/png"}]}
    assert ObscuraBackend._image_block(result)["data"] == "d"
    assert ObscuraBackend._image_block({"content": [{"type": "text"}]}) is None


def test_default_url_is_loopback():
    # The MCP port is an unauthenticated browser driver; its default must be
    # loopback and never off-host.
    assert DEFAULT_OBSCURA_URL == "http://127.0.0.1:3000"


# ── SSE / plain-JSON decoding (no socket) ────────────────────────────────────

def test_decode_body_plain_json():
    b = ObscuraBackend()
    obj = b._decode_body("application/json", b'{"jsonrpc":"2.0","id":1,"result":{}}')
    assert obj["result"] == {}


def test_decode_body_sse_data_lines():
    # The live transport answers plain JSON for a POST; SSE is parsed anyway so a
    # future streaming server does not read as "returned nothing".
    b = ObscuraBackend()
    sse = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
    obj = b._decode_body("text/event-stream", sse)
    assert obj["result"]["ok"] is True


def test_decode_body_non_json_raises():
    b = ObscuraBackend()
    with pytest.raises(BrowseError):
        b._decode_body("application/json", b"<html>not json</html>")


# ── the fake MCP server (mirrors the CLI self-test) ──────────────────────────

_FAKE_SNAPSHOT = _SNAPSHOT
_FAKE_MARKDOWN = "# Example Domain\n\nExample body text here."
_FAKE_ELEMENTS = ('ref=e1    a                      "More information..." name="more"\n'
                  'ref=e2    button                 "OK"')
_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def _handler():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            return

        def _send(self, obj, status=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            if self.path != "/mcp":
                self._send({"error": "not found"}, 404)
                return
            n = int(self.headers.get("Content-Length", 0))
            msg = json.loads(self.rfile.read(n) or b"{}")
            rid, method = msg.get("id"), msg.get("method")
            if method == "initialize":
                self._send({"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "0"}}})
                return
            if method == "tools/call":
                p = msg.get("params", {})
                name, args = p.get("name"), p.get("arguments", {})
                url = args.get("url", "")
                if name == "browser_navigate" and url == "jsonrpc://error":
                    self._send({"jsonrpc": "2.0", "id": rid,
                                "error": {"code": -32000, "message": "boom"}})
                    return
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
                    self._send({"jsonrpc": "2.0", "id": rid,
                                "result": {"content": [{"type": "text", "text": text}]}})
                    return
                if name == "browser_screenshot":
                    self._send({"jsonrpc": "2.0", "id": rid, "result": {
                        "content": [{"type": "image", "data": _PNG_B64,
                                     "mimeType": "image/png"}]}})
                    return
                self._send({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": f"Error: Unknown tool: {name}"}],
                    "isError": True}})
                return
            self._send({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32601, "message": "no"}})
    return Handler


@pytest.fixture()
def obscura():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler())
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield ObscuraBackend(f"http://127.0.0.1:{port}", timeout=5.0)
    finally:
        server.shutdown()
        server.server_close()


def test_browse_returns_a_page_tagged_obscura(obscura):
    page = obscura.browse("https://example.com")
    assert isinstance(page, Page)
    assert page.engine == "obscura"       # the required tag
    assert page.text == "Example body text here."
    assert page.ok


def test_browse_maps_screenshot_to_page(obscura):
    page = obscura.browse("https://example.com", text=False, screenshot=True)
    assert page.screenshot == _PNG_B64


def test_markdown_round_trips(obscura):
    assert obscura.markdown("https://example.com") == _FAKE_MARKDOWN


def test_observe_returns_observation_with_elements(obscura):
    obs = obscura.observe("https://example.com")
    assert isinstance(obs, Observation)
    assert obs.title == "Example Domain"
    assert obs.text == "Example body text here."
    assert obs.elements[0]["ref"] == "e1"


def test_interactive_elements_navigates_and_parses(obscura):
    els = obscura.interactive_elements("https://example.com")
    assert [e["ref"] for e in els] == ["e1", "e2"]


# ── mutation guards: a failure must RAISE, never return an empty page ─────────

def test_jsonrpc_error_raises_browse_error(obscura):
    # If the client returned "" here, a dead call and a blank page would be one
    # value to every caller — the silence this backend refuses.
    with pytest.raises(BrowseError):
        obscura.browse("jsonrpc://error")


def test_tool_iserror_result_raises_browse_error(obscura):
    # A tool failure is an HTTP 200 with isError:true and "Error: ..." text. The
    # backend must raise on it, not hand the error string back as content.
    with pytest.raises(BrowseError):
        obscura.browse("tool://error")


def test_transport_down_raises_browse_error():
    # Nothing listening on this port: a refused connection reads as "down" and
    # must surface, not become an empty page.
    down = ObscuraBackend("http://127.0.0.1:1", timeout=2.0)
    with pytest.raises(BrowseError):
        down.browse("https://example.com")
