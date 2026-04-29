"""Smoke tests for the v0.1 LSP server.

Every test exercises one user-visible behaviour. Tests will grow in Week 2 alongside
features. We deliberately avoid mocking ``systemrdl-compiler`` — eng review #2 (decision
log) requires real-elaboration coverage.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest
from systemrdl.messages import Severity

from systemrdl_lsp.server import _elaborate

VALID_RDL = textwrap.dedent("""
    addrmap simple {
        reg {
            field {
                sw = rw;
                hw = r;
            } enable;
        } CTRL @ 0x0;
    };
""").strip()

INVALID_RDL = textwrap.dedent("""
    addrmap broken {
        not_a_keyword;
    };
""").strip()


@pytest.fixture
def tmp_rdl(tmp_path):
    def _write(content: str, name: str = "x.rdl") -> pathlib.Path:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p

    return _write


def test_valid_file_produces_no_errors(tmp_rdl):
    """A valid addrmap elaborates clean — zero captured messages."""
    msgs = _elaborate(tmp_rdl(VALID_RDL))
    errors = [m for m in msgs if m[0] in (Severity.ERROR, Severity.FATAL)]
    assert errors == [], f"expected no errors on valid file; got {errors}"


def test_invalid_file_reports_error_with_location(tmp_rdl):
    """An invalid file produces at least one error message with a source ref."""
    msgs = _elaborate(tmp_rdl(INVALID_RDL))
    errors = [m for m in msgs if m[0] in (Severity.ERROR, Severity.FATAL)]
    assert errors, "expected at least one error on invalid file"

    sev, text, src_ref = errors[0]
    assert src_ref is not None, "first error should carry a source reference"
    assert text, "error text must be non-empty"


def test_src_ref_resolves_to_correct_line(tmp_rdl):
    """Regression: error on file line N must yield LSP Range with line=(N-1).

    Previously ``_src_ref_to_range`` looked for non-existent ``start_line``/``start_col``
    attributes and silently fell back to line 1, so every diagnostic appeared on the first
    line of the file.
    """
    from systemrdl_lsp.server import _src_ref_to_range

    rdl_with_error_on_line_3 = "addrmap a {\n    reg {} CTRL;\n    junk_token;\n};\n"
    msgs = _elaborate(tmp_rdl(rdl_with_error_on_line_3, "x.rdl"))
    errors = [m for m in msgs if m[0] in (Severity.ERROR, Severity.FATAL) and m[2] is not None]
    assert errors, "expected an error with a source ref"

    rng = _src_ref_to_range(errors[0][2])
    # LSP is 0-based, file line 3 = LSP line 2.
    assert rng.start.line == 2, f"expected LSP line 2 (file line 3), got {rng.start.line}"
    assert rng.end.character > rng.start.character, "range must be non-empty"


def test_missing_file_returns_message_not_crash(tmp_path):
    """Calling ``_elaborate`` on a non-existent path captures an error rather than raising."""
    missing = tmp_path / "does-not-exist.rdl"
    msgs = _elaborate(missing)
    # systemrdl-compiler raises an internal error which our printer captures, plus our
    # defensive except clause may add an "internal:" message. Either way: no exception, ≥1 message.
    assert msgs, "expected at least one captured message for a missing file"
