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
    "BrowseClient", "BrowseError", "Page", "Observation", "Session",
    "BROWSE_FIELDS", "SHOT_KEY", "ACTIONS", "NETWORK_KEY",
    "ACT_FIELDS", "SESSION_OPEN_FIELDS",
    "browse_body", "act_body",
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

#: The key /session/{sid}/network answers with. MEASURED: the response is
#: {"count": N, "responses": [...]}. The obvious guess is "requests" — it is a
#: NETWORK endpoint — and that guess returns [] on every call, forever, with a
#: 200 and no error. The whole reason to open a session while debugging is to see
#: what the page really asked for, so reading the wrong key turns the one tool
#: that shows you the answer into one that silently insists there is nothing to
#: see. Same class as SHOT_KEY, one route over.
NETWORK_KEY = "responses"

#: Exactly the actions the service dispatches on. An unknown action falls through
#: its if/elif chain, so it is not an error — it is a no-op that returns
#: successfully, which reads as "I clicked and nothing happened".
ACTIONS = ("goto", "click", "click_xy", "fill", "press", "select", "wait",
           "scroll", "screenshot", "eval", "network", "console")

#: Fields SessionOpenRequest declares, including the inherited robots controls.
SESSION_OPEN_FIELDS = ("url", "storage_state", "headless", "viewport_width",
                       "viewport_height", "gpu", "capture_console",
                       "obey_robots", "robots_override_reason")

#: Fields SessionActRequest declares (it also inherits the robots controls).
ACT_FIELDS = ("action", "selector", "text", "value", "x", "y", "timeout_ms")


def act_body(action: str, **kwargs: Any) -> dict:
    """The request body for /session/{sid}/act. Pure, so it is testable offline.

    Refuses an unknown action rather than sending it: the server's dispatch is an
    if/elif chain with no else, so a typo'd action is accepted, does nothing, and
    answers 200.
    """
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}, got {action!r}")
    unknown = [k for k in kwargs if k not in ACT_FIELDS
               and k not in ("obey_robots", "robots_override_reason")]
    if unknown:
        raise ValueError(f"{action}: unknown field(s) {unknown} — declared: {ACT_FIELDS}")
    return {"action": action, **kwargs}


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


class Observation:
    """What /session/{sid}/observe returns — a DIFFERENT shape from /browse.

    Measured: {url, title, elements, text, screenshot_base64}. It has a `title`
    and a list of interactive `elements` that /browse does not, and it always
    carries a screenshot whether or not one was asked for. Parsing it as a `Page`
    would silently discard `elements`, which is the field the whole OODA loop
    turns on — an agent would be deciding what to click from a list it never saw.
    """

    __slots__ = ("url", "title", "text", "elements", "screenshot", "raw")

    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.url: str = raw.get("url", "")
        self.title: str = raw.get("title", "")
        self.text: str = raw.get("text") or ""
        self.elements: list[dict] = raw.get("elements") or []
        self.screenshot: Optional[str] = raw.get(SHOT_KEY) or None

    def __repr__(self) -> str:
        return f"<Observation {self.url!r} {len(self.elements)} elements>"


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
        """Drive the session. `action` must be one of ACTIONS — see act_body."""
        return self._client._post(f"/session/{self.sid}/act", act_body(action, **kwargs))

    # Named shorthands for the actions whose argument is easy to get wrong: the
    # URL for `goto` and the text for `fill` both go in `value`, not in `text`
    # (which is the CLICK-BY-VISIBLE-TEXT selector). Sending a url as `text`
    # is accepted, ignored, and answers 200.
    def goto(self, url: str, **kw: Any) -> dict:
        return self.act("goto", value=url, **kw)

    def click(self, selector: str = "", *, text: str = "", **kw: Any) -> dict:
        return self.act("click", selector=selector, text=text, **kw)

    def fill(self, selector: str, value: str, **kw: Any) -> dict:
        return self.act("fill", selector=selector, value=value, **kw)

    def observe(self) -> Observation:
        """Read the current page back: url, title, text, elements, screenshot.

        Takes no arguments — the route accepts none, and a client offering
        kwargs here would let a caller pass options that go nowhere.
        """
        return Observation(self._client._post(f"/session/{self.sid}/observe", {}))

    def network(self, clear: bool = True) -> list[dict]:
        """The JSON-ish XHR/fetch responses the page made.

        The reason to reach for a session over a one-shot browse when debugging: a
        page that renders correctly and fails silently is usually failing in a request
        nobody looked at.

        TWO THINGS THAT MAKE AN EMPTY LIST MEAN NOTHING, both measured:

        * **It is FILTERED, not a full request log.** The server records a response
          only when the content-type says json, or the URL contains `/api/` or ends
          `.json` — and never for status >= 400 or an OPTIONS preflight. So a page
          that fetched HTML, or whose API call 500'd, shows up as no traffic at
          all. Absence here is not evidence the request was not made.
        * **The default DRAINS.** The server clears the buffer as it answers, so a
          second consecutive call returns nothing (measured: 1 then 0). Pass
          `clear=False` to look without draining — otherwise a second look reads
          as "the page stopped making requests".
        """
        got = self._client._get(
            f"/session/{self.sid}/network" + ("" if clear else "?clear=false"))
        if isinstance(got, list):
            return got
        return got.get(NETWORK_KEY) or []

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
        """Open a persistent session — the route that can carry credentials.

        Declared fields (measured): url, storage_state, headless, viewport_width,
        viewport_height, gpu, capture_console, plus the robots controls. Unknown
        ones are refused here rather than sent: this model does NOT forbid
        extras, so the server would drop a misspelled `storage_state` in silence
        and hand back an anonymous session that looks logged in — the exact
        defect /browse's extra="forbid" exists to prevent, one route over.
        """
        unknown = [k for k in kwargs if k not in SESSION_OPEN_FIELDS]
        if unknown:
            raise ValueError(
                f"unknown field(s) {unknown} — declared: {SESSION_OPEN_FIELDS}. "
                "This route ignores extras rather than rejecting them, so sending "
                "one yields a session that quietly is not what you asked for.")
        got = self._post("/session/open", kwargs)
        sid = got.get("session_id") or got.get("sid")
        if not sid:
            raise BrowseError(f"/session/open returned no session id: {got!r}")
        return Session(self, sid)

    def sessions(self) -> list[dict]:
        got = self._get("/session/list")
        return got if isinstance(got, list) else got.get("sessions", [])
