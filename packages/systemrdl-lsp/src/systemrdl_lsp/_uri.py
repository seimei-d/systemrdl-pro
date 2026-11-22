"""URI ↔ filesystem path conversion."""

from __future__ import annotations

import os
import pathlib
import urllib.parse


def _normalize_uri_path(path: str, *, windows: bool) -> str:
    """Decode a file URI path and restore the drive prefix Windows expects."""
    path = urllib.parse.unquote(path)
    if (
        windows
        and len(path) >= 3
        and path[0] == "/"
        and path[1].isascii()
        and path[1].isalpha()
        and path[2] == ":"
        and (len(path) == 3 or path[3] == "/")
    ):
        return path[1:]
    return path


def _uri_to_path(uri: str) -> pathlib.Path:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme not in ("file", ""):
        raise ValueError(f"Only file:// URIs are supported, got {uri!r}")
    return pathlib.Path(_normalize_uri_path(parsed.path, windows=os.name == "nt"))


def _path_to_uri(p: pathlib.Path) -> str:
    return p.resolve().as_uri()
