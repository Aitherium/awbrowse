"""awbrowse — a portable client for AitherBrowser-shaped page rendering.

    from awbrowse import BrowseClient

    b = BrowseClient("https://browser.example.com", token="...")
    page = b.browse("https://example.com")
    print(page.title, len(page.text))

For anything authenticated, use a session — `browse()` deliberately cannot carry
cookies. See `client.py` for why that is a feature and not a gap.
"""

from __future__ import annotations

from awbrowse.client import (
    ACT_FIELDS,
    ACTIONS,
    BROWSE_FIELDS,
    NETWORK_KEY,
    SESSION_OPEN_FIELDS,
    SHOT_KEY,
    BrowseClient,
    BrowseError,
    Observation,
    Page,
    Session,
    act_body,
    browse_body,
)

__version__ = "0.1.0"

__all__ = [
    "BrowseClient",
    "BrowseError",
    "Page",
    "Observation",
    "Session",
    "BROWSE_FIELDS",
    "SHOT_KEY",
    "NETWORK_KEY",
    "ACTIONS",
    "ACT_FIELDS",
    "SESSION_OPEN_FIELDS",
    "browse_body",
    "act_body",
    "__version__",
]
