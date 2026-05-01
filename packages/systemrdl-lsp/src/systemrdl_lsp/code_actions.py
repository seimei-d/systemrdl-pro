"""textDocument/codeAction: surface quick fixes the user can invoke from the
lightbulb.

For now, one fix:

- **Add missing reset**. SystemRDL fields without an explicit reset value are
  legal but typically a mistake — undefined hardware reset is a frequent bug
  source. The action inserts ``= 0`` before the closing ``;`` on the field's
  instantiation line.

The quick fix is offered when the cursor sits on a field instantiation line
that ends with ``[N:N];`` or ``[N:N]\s*;`` (no ``=`` after the bit range).
"""

from __future__ import annotations

import re

from lsprotocol.types import (
    CodeAction,
    CodeActionKind,
    Position,
    Range,
    TextEdit,
    WorkspaceEdit,
)

# Field instantiation: end of line is ``[msb:lsb] ;`` with no ``=`` between
# the closing bracket and the semicolon. We capture the position of ``;`` so
# we can insert ``= 0`` immediately before it.
_FIELD_NO_RESET_RE = re.compile(
    r"\][^=;]*?(?<!=)(\s*);"
)


def _add_missing_reset_action(
    uri: str, text: str, line_0b: int, _char_0b: int
) -> CodeAction | None:
    """Build the "Add missing reset" CodeAction for the line under cursor.

    Returns None when the line doesn't end with a field instantiation that's
    missing its reset.
    """
    lines = text.splitlines()
    if line_0b < 0 or line_0b >= len(lines):
        return None
    line = lines[line_0b]
    if "=" in line and re.search(r"=\s*[0-9xXbB]", line.split("//", 1)[0]):
        # Already has a numeric reset somewhere on the line — bail.
        return None
    m = _FIELD_NO_RESET_RE.search(line)
    if m is None:
        return None
    # Insert ``= 0`` immediately before the semicolon. Use a zero-width
    # range at the semicolon position; LSP TextEdit treats zero-width
    # ranges as insertions.
    semi_col = m.end() - 1
    # Indent-aware: drop a single space if there isn't one already so the
    # output reads ``[0:0] = 0;`` rather than ``[0:0]= 0;``.
    needs_space = semi_col == 0 or line[semi_col - 1] != " "
    new_text = (" = 0" if needs_space else "= 0") if not needs_space else " = 0"
    edit = TextEdit(
        range=Range(
            start=Position(line=line_0b, character=semi_col),
            end=Position(line=line_0b, character=semi_col),
        ),
        new_text=new_text,
    )
    return CodeAction(
        title="Add `= 0` reset value",
        kind=CodeActionKind.QuickFix,
        edit=WorkspaceEdit(changes={uri: [edit]}),
    )


def _code_actions_for_range(uri: str, text: str, rng: Range) -> list[CodeAction]:
    """Produce the list of applicable CodeActions for the given range.

    LSP sends a Range; we iterate every line in it and collect any actions
    that apply. Most invocations come with a zero-width range at the cursor
    so this is usually a one-line scan.
    """
    actions: list[CodeAction] = []
    for line_0b in range(rng.start.line, rng.end.line + 1):
        action = _add_missing_reset_action(uri, text, line_0b, rng.start.character)
        if action is not None:
            actions.append(action)
    return actions


__all__ = ["_add_missing_reset_action", "_code_actions_for_range"]
