"""A portable client for an AitherBrowser-shaped service.

WHY A CLIENT AND NOT A LIFT
===========================
`AitherBrowser.py` is 1,199 lines with 11 monorepo imports. Renaming it into a
package produces something that raises `ModuleNotFoundError` on a stranger's machine
while reading as authoritative — a broken package, not a shipped one.

`awrelay` already established the alternative and it is the right one: it did not lift
the 19k-line relay server, it shipped a small standalone CLIENT that speaks to any
relay-shaped server. This is that move for browsing. The engine stays heavy, private
and free to change; the wire contract is what ships.

WHAT "SHAPED" MEANS
===================
Any service exposing these routes. They are not invented — they are read off the
running service:

    POST /browse                    {url, wait_time, extract_text, screenshot}
    POST /scrape                    structured extraction
    POST /session/open              a persistent, authenticated session
    POST /session/{sid}/act         click / type / navigate within it
    POST /session/{sid}/observe     read the page back
    GET  /session/{sid}/network     what the page actually requested
    POST /session/{sid}/close

THE FIELD SET IS EXACT, ON PURPOSE
==================================
The service declares `extra="forbid"` on its browse model, and the comment there
records why: Pydantic's default `extra="ignore"` silently DROPPED `session_cookies`,
`cookies`, `storage_state` and `headers`, so an "authenticated" render ran anonymous
and returned the login page as if it were the app. A client that invents a field name
gets a 422; a client that invents one the server ignores gets a wrong answer quietly.
So this sends exactly the declared fields, and authenticated work goes through
`open_session`, which is the route that actually carries credentials.

Depends on httpx and nothing else.
"""
from __future__ import annotations

import warnings
from typing import Any, Optional

__all__ = [
    "BrowseClient", "BrowseError", "Page", "Session",
    "BROWSE_FIELDS", "SHOT_KEY", "browse_body",
]

DEFAULT_TIMEOUT = 60.0

#: EXACTLY the fields the service's browse model declares. Not a superset and not
#: a subset: the model is `extra="forbid"`, so one extra key is a 422 for the whole
#: request. This is a constant rather than an inline literal so the self-test can
#: assert the real thing instead of a copy of it.
BROWSE_FIELDS = ("url", "wait_time", "extract_text", "screenshot")


def browse_body(url: str, wait_ms: int = 2000, text: bool = True,
                screenshot: bool = False) -> dict:
    """The request body for POST /browse. Pure, so it is testable with no service."""
    return {
        "url": url,
        "wait_time": wait_ms,
        "extract_text": text,
        "screenshot": screenshot,
    }


class BrowseError(RuntimeError):
    """The service refused or could not answer.

    Raised rather than returned: a browse that failed and a page that was genuinely
    empty are different outcomes, and a client that returns `""` for both makes them
    indistinguishable to the caller.
    """


#: The screenshot key the service actually sends. MEASURED against a running
#: AitherBrowser, not inferred from the REQUEST field, which is `screenshot`.
#: They differ, and reading the request name yields None on every successful
#: capture — a client that "returns no screenshot" forever while the service
#: sends one every time. Nothing offline could have caught this: the request is
#: accepted, the response is a 200, and the missing key is simply absent.
SHOT_KEY = "screenshot_base64"


class Page:
    """One rendered page.

    The attributes are exactly what the service was measured to return —
    `status`, `url`, `engine`, `content`, plus `screenshot_base64` when a capture
    was asked for. Deliberately no `title` or `html`: this route sends neither,
    and an attribute that is empty on every response is worse than an absent one,
    because callers branch on it. Anything else the service adds is on `.raw`.
    """

    __slots__ = ("url", "text", "status", "engine", "screenshot", "raw")

    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.url: str = raw.get("url", "")
        # `content` is what this service sends; `text` is accepted because the
        # session/observe route spells it that way.
        self.text: str = raw.get("content") or raw.get("text") or ""
        self.status: str = raw.get("status", "")
        self.engine: str = raw.get("engine", "")
        # base64 when the caller asked for one; None otherwise. Never "" — an empty
        # string reads as "a screenshot that came back blank".
        self.screenshot: Optional[str] = raw.get(SHOT_KEY) or raw.get("screenshot") or None

    @property
    def ok(self) -> bool:
        """True unless the service reported a failed render.

        A service that answers 200 with `status: "error"` and no content would
        otherwise arrive as an ordinary blank page.
        """
        return self.status in ("", "success", "ok")

    def __repr__(self) -> str:
        return f"<Page {self.url!r} {len(self.text)} chars via {self.engine or '?'}>"


class Session:
    """A persistent browser session — the route that carries credentials.

    Use this for anything authenticated. `browse()` deliberately cannot take cookies:
    the server forbids unknown fields on that model precisely because silently dropping
    them produced anonymous renders that looked like successful ones.
    """

    def __init__(self, client: "BrowseClient", sid: str) -> None:
        self._client = client
        self.sid = sid

    def act(self, action: str, **kwargs: Any) -> dict:
        """Click, type, navigate — whatever the service supports."""
        return self._client._post(f"/session/{self.sid}/act", {"action": action, **kwargs})

    def observe(self, **kwargs: Any) -> Page:
        """Read the current page back."""
        return Page(self._client._post(f"/session/{self.sid}/observe", kwargs))

    def network(self) -> list[dict]:
        """What the page actually requested.

        The reason to reach for a session over a one-shot browse when debugging: a
        page that renders correctly and fails silently is usually failing in a request
        nobody looked at.
        """
        got = self._client._get(f"/session/{self.sid}/network")
        return got if isinstance(got, list) else got.get("requests", [])

    def close(self) -> None:
        self._client._post(f"/session/{self.sid}/close", {})

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: object) -> None:
        # Sessions hold a real browser context, so a close that fails LEAKS A REAL
        # PROCESS on the service. Best-effort, because raising here would replace
        # whatever exception the caller's block was already carrying — but never
        # silent: swallowed, this reads as "sessions are cleaned up" while they
        # accumulate until the service runs out of room.
        try:
            self.close()
        except BrowseError as exc_close:
            warnings.warn(f"awbrowse: session {self.sid} was not closed: {exc_close}",
                          RuntimeWarning, stacklevel=2)


class BrowseClient:
    """Talks to an AitherBrowser-shaped service."""

    def __init__(self, base_url: str, token: Optional[str] = None, *,
                 timeout: float = DEFAULT_TIMEOUT, verify: bool | str = True) -> None:
        """
        base_url  the service origin. In-network these serve TLS; plain http against a
                  TLS listener closes the socket and reads as "the service is down"
                  while it is perfectly healthy.
        token     a Bearer token. The CALLER's, never a service credential: this
                  package ships publicly, so an internal key would either fail for
                  external callers or work for everyone who reads the source.
        verify    never pass False against a real deployment — trust the CA instead.
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.verify = verify

    def _client(self):
        import httpx  # local import so the module is importable without httpx present

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return httpx.Client(base_url=self.base_url, headers=headers,
                            timeout=self.timeout, verify=self.verify)

    def _post(self, path: str, body: dict) -> dict:
        try:
            with self._client() as c:
                r = c.post(path, json=body)
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            raise BrowseError(f"{path}: {exc}") from exc
        if r.status_code >= 400:
            raise BrowseError(f"{path}: HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def _get(self, path: str) -> Any:
        try:
            with self._client() as c:
                r = c.get(path)
        except Exception as exc:  # noqa: BLE001
            raise BrowseError(f"{path}: {exc}") from exc
        if r.status_code >= 400:
            raise BrowseError(f"{path}: HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    # ── One-shot ────────────────────────────────────────────────────────────

    def browse(self, url: str, *, wait_ms: int = 2000, text: bool = True,
               screenshot: bool = False) -> Page:
        """Render one page and return it.

        ONLY these four fields are sent. The service declares extra="forbid" on this
        model, so anything else is a loud 422 rather than a silent drop — which is the
        whole reason the model is strict. For cookies or a stored session, use
        `open_session`.
        """
        page = Page(self._post("/browse", browse_body(url, wait_ms, text, screenshot)))
        if not page.ok:
            # A render the service itself calls failed. Returned, it is a Page
            # with empty text — indistinguishable from a page that really is
            # blank, which is the silence this client exists to refuse.
            raise BrowseError(f"/browse: the service reported status={page.status!r} "
                              f"for {url}")
        return page

    def scrape(self, url: str, **kwargs: Any) -> dict:
        """Structured extraction, when the service supports a schema for this site."""
        return self._post("/scrape", {"url": url, **kwargs})

    # ── Sessions ────────────────────────────────────────────────────────────

    def open_session(self, **kwargs: Any) -> Session:
        """Open a persistent session — the route that can carry credentials."""
        got = self._post("/session/open", kwargs)
        sid = got.get("session_id") or got.get("sid")
        if not sid:
            raise BrowseError(f"/session/open returned no session id: {got!r}")
        return Session(self, sid)

    def sessions(self) -> list[dict]:
        got = self._get("/session/list")
        return got if isinstance(got, list) else got.get("sessions", [])
