"""URI ↔ filesystem path conversion."""

from __future__ import annotations

import pathlib
import urllib.parse


def _uri_to_path(uri: str) -> pathlib.Path:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme not in ("file", ""):
        raise ValueError(f"Only file:// URIs are supported, got {uri!r}")
    return pathlib.Path(urllib.parse.unquote(parsed.path))


def _path_to_uri(p: pathlib.Path) -> str:
    return p.resolve().as_uri()
