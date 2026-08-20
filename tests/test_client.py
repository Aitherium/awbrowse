"""What a stranger's machine must still be true of awbrowse.

These overlap the CLI `--self-test` on purpose. The self-test is what ships (it
runs on any install, with no pytest and no network); this is what runs in CI with
the mutation guards that prove each assertion can still fail.
"""
from __future__ import annotations

import pytest
from awbrowse import (
    BROWSE_FIELDS,
    SHOT_KEY,
    BrowseClient,
    BrowseError,
    Page,
    browse_body,
)
from awbrowse.cli import main


def test_browse_body_is_exactly_the_declared_field_set():
    # The service model is extra="forbid": one extra key 422s the whole request.
    # Asserting the exact tuple (not just membership) is what catches an addition.
    assert tuple(browse_body("https://x")) == BROWSE_FIELDS


def test_browse_body_defaults_match_the_service_defaults():
    assert browse_body("https://x") == {
        "url": "https://x",
        "wait_time": 2000,
        "extract_text": True,
        "screenshot": False,
    }


@pytest.mark.parametrize("raw,expected", [
    ({"content": "b"}, "b"),               # what /browse actually sends
    ({"text": "a"}, "a"),                  # what session/observe spells it
    ({"content": "", "text": "a"}, "a"),   # an empty one falls through
    ({}, ""),
])
def test_page_text_fallback_chain(raw, expected):
    assert Page(raw).text == expected


def test_screenshot_reads_the_response_key_not_the_request_field():
    """The request field is `screenshot`; the response key is `screenshot_base64`.

    MEASURED live against a running AitherBrowser, and it is the one defect no
    offline test could have found: the request is valid, the response is a 200,
    and the key you expected is simply absent. Reading the request name yields
    None on every successful capture, so `awbrowse shot` reports "the service
    returned no screenshot" forever while the service sends one every time.
    """
    assert SHOT_KEY == "screenshot_base64"
    assert Page({SHOT_KEY: "iVBOR"}).screenshot == "iVBOR"


def test_absent_screenshot_is_none_not_empty_string():
    # MUTATION GUARD: `raw.get(SHOT_KEY, "")` passes an is-falsy test and makes
    # "no screenshot was requested" indistinguishable from "the screenshot came
    # back blank" to every downstream caller.
    assert Page({}).screenshot is None
    assert Page({SHOT_KEY: ""}).screenshot is None


def test_a_failed_render_is_not_reported_as_a_blank_page():
    # The service can answer 200 with status != success. Parsed as an ordinary
    # Page, that arrives as empty text — a failure wearing a blank page's face.
    assert Page({"status": "error"}).ok is False
    assert Page({"status": "success"}).ok is True
    assert Page({}).ok is True          # status-less responses are not failures


def test_page_exposes_only_fields_the_service_actually_sends():
    # No `title`, no `html`: this route sends neither, and an attribute that is
    # empty on every single response is worse than an absent one because callers
    # branch on it. Everything else the service adds is reachable on .raw.
    p = Page({"status": "success", "url": "u", "engine": "playwright", "content": "c"})
    assert (p.url, p.engine, p.text) == ("u", "playwright", "c")
    assert not hasattr(p, "title") and not hasattr(p, "html")


def test_base_url_is_normalised():
    assert BrowseClient("https://h/").base_url == "https://h"
    assert BrowseClient("https://h").base_url == "https://h"


def test_no_token_means_no_header_never_an_empty_one():
    # An empty Bearer is rejected differently from an absent one, which sends
    # you debugging the auth server instead of your own config.
    assert BrowseClient("https://h").token is None


def test_transport_failure_raises_rather_than_returning_an_empty_page():
    # MUTATION GUARD: if _post swallowed the error and returned {}, a dead
    # service and a blank page would be the same value to the caller.
    c = BrowseClient("http://127.0.0.1:9")  # nothing listens on discard
    with pytest.raises(BrowseError):
        c.browse("https://example.com")


def test_self_test_passes_and_is_the_shipped_check():
    assert main(["--self-test"]) == 0


def test_no_subcommand_is_an_error_not_a_silent_success():
    assert main([]) == 2
