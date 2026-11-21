"""The web page's Content Security Policy (``policy``), sent with every page: what a page
may load and run. Text that reaches a page (what a visitor said, a name) is escaped, and
this is the second line: even text that slipped through couldn't run as a script.

- **Scripts** only from Gatekeeper's own files (``webapp/``), never inline, in an
  attribute (``onclick``) or from ``eval``.
- **Styles**: each page's own ``<style>``, allowed by its hash (``style``); none in
  attributes.
- **Pictures, clips, requests, the app's manifest and service worker**: Gatekeeper's own.
- **Forms** post only to Gatekeeper, only its own pages may show it in a frame (Home
  Assistant's ingress serves it from Home Assistant's own address, so that's its own), and
  there's no ``<base>`` and no plugins.
"""

from __future__ import annotations

import base64
import hashlib
import threading

_lock = threading.Lock()
_styles: set[str] = set()  # the pages' <style>s, as CSP hashes


def style(css: str) -> str:
    """A page's ``<style>``, allowed by its hash."""
    digest = base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()
    with _lock:
        _styles.add(f"'sha256-{digest}'")
    return f"<style>{css}</style>"


def policy() -> str:
    with _lock:
        styles = " ".join(sorted(_styles)) or "'none'"
    return "; ".join(
        [
            "default-src 'none'",
            "script-src 'self'",
            f"style-src {styles}",
            "img-src 'self'",
            "media-src 'self'",
            "connect-src 'self'",
            "manifest-src 'self'",
            "worker-src 'self'",
            "form-action 'self'",
            "frame-ancestors 'self'",
            "base-uri 'none'",
        ]
    )
