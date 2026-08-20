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
    BROWSE_FIELDS,
    SHOT_KEY,
    BrowseClient,
    BrowseError,
    Page,
    Session,
    browse_body,
)

__version__ = "0.1.0"

__all__ = [
    "BrowseClient",
    "BrowseError",
    "Page",
    "Session",
    "BROWSE_FIELDS",
    "SHOT_KEY",
    "browse_body",
    "__version__",
]
