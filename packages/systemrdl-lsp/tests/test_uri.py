"""Tests for URI ↔ filesystem path conversion."""

from systemrdl_lsp._uri import _normalize_uri_path


def test_windows_drive_uri_restores_drive_prefix():
    assert _normalize_uri_path("/t%3A/main/docs/top.rdl", windows=True) == (
        "t:/main/docs/top.rdl"
    )


def test_windows_drive_uri_only_strips_root_before_drive():
    assert _normalize_uri_path("/shared/main.rdl", windows=True) == "/shared/main.rdl"
    assert _normalize_uri_path("/t:relative/main.rdl", windows=True) == (
        "/t:relative/main.rdl"
    )


def test_posix_path_keeps_leading_slash_before_drive_like_segment():
    assert _normalize_uri_path("/t%3A/main/docs/top.rdl", windows=False) == (
        "/t:/main/docs/top.rdl"
    )
