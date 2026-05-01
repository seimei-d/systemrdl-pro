"""textDocument/completion: keyword + property + value catalogues + user-defined types.

Static catalogue: label → one-line markdown shown in the completion popup's
detail panel. Coverage is intentionally narrower than the full SystemRDL 2.0
spec — we cover the properties and access modes that matter for ~95% of real
register definitions.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from lsprotocol.types import CompletionItem, CompletionItemKind

from .definition import _comp_defs_from_cached

if TYPE_CHECKING:
    from systemrdl.node import RootNode


SYSTEMRDL_TOP_KEYWORDS: dict[str, str] = {
    "addrmap": "Top-level address map. Wraps registers/regfiles into an addressable hierarchy.",
    "regfile": "Logical group of registers sharing a base address.",
    "reg": "Hardware register. Contains 1+ fields packed into `regwidth` bits (default 32).",
    "field": "Bit field inside a register. Has `sw` and `hw` access modes plus a reset value.",
    "enum": "Enumerated set of values, usable as a field value.",
    "mem": "External memory region (no internal storage, uses `external` accessor).",
    "signal": "External signal — wired to/from logic outside the register block.",
    "external": "Marks an instance as external — backing logic lives outside the generated RTL.",
    "internal": "Marks an instance as internal (default; usually omitted).",
    "bridge": (
        "Marks this `addrmap` as a bus bridge. RTL backends use it as a synthesis "
        "hint to wire two address spaces together (clause 9.2)."
    ),
    "abstract": "Modifier — definition without an instance (cannot be elaborated alone).",
    "alias": "Alias register — mirrors another reg by address; writes propagate (clause 10.5).",
    "default": "Default-property assignment — applies to every later sibling unless overridden.",
    "property": "User-defined property declaration.",
    "constraint": "User-defined constraint declaration (rarely used).",
    "true": "Boolean literal `true`.",
    "false": "Boolean literal `false`.",
}

SYSTEMRDL_PROPERTIES: dict[str, str] = {
    # Component metadata
    "name": 'Human-readable name shown in docs/viewers, e.g. `name = "Control register"`.',
    "desc": 'Long-form description, may contain multi-line markdown.',
    # Field access semantics
    "sw": "Software access mode. Values: `rw`, `ro`, `wo`, `r`, `w`, `na`.",
    "hw": "Hardware access mode. Values: `rw`, `ro`, `wo`, `r`, `w`, `na`.",
    "reset": "Reset value (hex, dec, or binary). Applied on system reset.",
    "resetsignal": "Override the reset signal driving this field.",
    "rclr": "On software read: clear the field to 0.",
    "rset": "On software read: set the field to all-ones.",
    "ruser": "Custom on-read action (user-defined).",
    "onread": "Read-side effect. Common values: `rclr`, `rset`, `ruser`.",
    "onwrite": (
        "Write-side effect. Common: `woclr`, `woset`, `wzc`, `wzs`, `wclr`, `wset`, `wuser`."
    ),
    "swacc": "Status flag: software just accessed (read or write).",
    "swmod": "Status flag: software just modified the field's value.",
    "swwe": "Software write-enable signal.",
    "swwel": "Software write-enable, active-low.",
    "we": "Hardware write-enable.",
    "wel": "Hardware write-enable, active-low.",
    "anded": "Bitwise-AND output of all bits in the field.",
    "ored": "Bitwise-OR output of all bits.",
    "xored": "Bitwise-XOR output of all bits.",
    "fieldwidth": "Force a field width independent of the bit-range.",
    "encode": "Reference an `enum` definition that names the legal values.",
    "singlepulse": "Field acts as a single-cycle strobe — auto-clears next cycle.",
    # Register
    "regwidth": "Register width in bits (default `32`, also legal: `8`, `16`, `64`).",
    "accesswidth": "Smallest access size in bits (must divide `regwidth`).",
    "shared": "Register is shared across multiple addrmap instances.",
    # Addrmap / regfile
    "alignment": "Force address alignment for child instances.",
    "sharedextbus": "All external children share one bus.",
    "errextbus": "External errors propagate to the bus.",
    "bigendian": "Use big-endian addressing.",
    "littleendian": "Use little-endian addressing (default).",
    "addressing": "Addressing mode: `compact`, `regalign`, `fullalign`.",
    "lsb0": "Bit 0 is the LSB (default).",
    "msb0": "Bit 0 is the MSB (uncommon).",
    # Counter
    "counter": "Field is an up/down counter.",
    "incr": "Increment input signal.",
    "decr": "Decrement input signal.",
    "incrwidth": "Width of the increment value.",
    "decrwidth": "Width of the decrement value.",
    "incrvalue": "Constant increment.",
    "decrvalue": "Constant decrement.",
    "saturate": "Saturate at min/max instead of wrapping.",
    "incrsaturate": "Saturate on overflow.",
    "decrsaturate": "Saturate on underflow.",
    "threshold": "Threshold flag triggers when the counter crosses the value.",
    "incrthreshold": "Threshold for increment direction.",
    "decrthreshold": "Threshold for decrement direction.",
    "overflow": "Status: increment overflowed.",
    "underflow": "Status: decrement underflowed.",
    # Interrupt
    "intr": "Field is an interrupt source.",
    "enable": "Interrupt enable mask.",
    "mask": "Interrupt mask (when `intr` is set).",
    "haltenable": "Halts further interrupts when set.",
    "haltmask": "Mask for halt-enable.",
    "stickybit": "Field bit sticks until cleared by software.",
    "sticky": "Whole field is sticky.",
    # Conditional / structural
    "ispresent": (
        "Conditional elaboration: `ispresent = false;` omits this component from "
        "the elaborated tree (clause 9.5). Use with `parameter` to gate optional features."
    ),
    "precedence": (
        "On simultaneous sw/hw write conflict: which side wins. "
        "Values: `sw`, `hw`. (Clause 8.5.5.)"
    ),
    "donttest": "Tooling hint — exclude this component from automated register-test sweeps.",
    "dontcompare": "Tooling hint — value not stable across runs; exclude from comparison checks.",
    "rsvdset": (
        "If true, all reserved bits in this register are guaranteed to "
        "read as 1 (default 0)."
    ),
    "rsvdsetX": "If true, reserved bits read as `X` (don't-care).",
    "arbiter": "Arbitration scheme on simultaneous external/internal write conflict.",
}

# sw / hw access — the right-hand side of `sw =` / `hw =`.
SYSTEMRDL_RW_VALUES: dict[str, str] = {
    "rw": "Read-write.",
    "ro": "Read-only — writes are ignored.",
    "wo": "Write-only — reads return 0.",
    "r":  "Readable (alias of `ro`).",
    "w":  "Writable (alias of `wo`).",
    "na": "No access — software can neither read nor write.",
}

SYSTEMRDL_ONWRITE_VALUES: dict[str, str] = {
    "woclr": "Write-1-to-clear: writing 1 to a bit clears it; writing 0 leaves it.",
    "woset": "Write-1-to-set: writing 1 to a bit sets it; writing 0 leaves it.",
    "wzc":   "Write-0-to-clear.",
    "wzs":   "Write-0-to-set.",
    "wclr":  "Any write clears the field.",
    "wset":  "Any write sets all field bits.",
    "wzt":   "Write-0-to-toggle.",
    "wuser": "User-defined write action.",
}

SYSTEMRDL_ONREAD_VALUES: dict[str, str] = {
    "rclr":  "Read-to-clear: read clears the field after returning the old value.",
    "rset":  "Read-to-set: read sets all bits after returning the old value.",
    "ruser": "User-defined read action.",
}

# `addressing = …` enum values (clause 13.4).
SYSTEMRDL_ADDRESSING_VALUES: dict[str, str] = {
    "compact":   "Children pack with no padding (default).",
    "regalign":  "Each child aligned to its own size.",
    "fullalign": "Each child aligned to the largest child's size.",
}

# `precedence = …` enum values.
SYSTEMRDL_PRECEDENCE_VALUES: dict[str, str] = {
    "sw": "Software write wins on simultaneous conflict.",
    "hw": "Hardware write wins (default).",
}


_COMPLETION_CONTEXT_RE = re.compile(
    r"\b(sw|hw|onwrite|onread|addressing|precedence)\s*=\s*\w*$"
)


def _completion_context(text: str, line_0b: int, char_0b: int) -> str:
    """Detect what the cursor is right of, so we can narrow the suggestion list.

    Returns ``"sw_value"`` / ``"hw_value"`` / ``"onwrite_value"`` /
    ``"onread_value"`` for property RHS contexts; ``"general"`` otherwise.
    Single-line match — SystemRDL property assignments rarely span lines.
    """
    lines = text.splitlines()
    if line_0b < 0 or line_0b >= len(lines):
        return "general"
    prefix = lines[line_0b][:char_0b]
    m = _COMPLETION_CONTEXT_RE.search(prefix)
    if m:
        return f"{m.group(1)}_value"
    return "general"


def _make_items(catalogue: dict[str, str], kind: CompletionItemKind) -> list[CompletionItem]:
    return [
        CompletionItem(label=label, kind=kind, detail=doc, documentation=doc)
        for label, doc in catalogue.items()
    ]


def _completion_items_static() -> list[CompletionItem]:
    """Full keyword + property + value catalogue with one-line docs.

    Used for the ``"general"`` context. Each item carries both ``detail`` and
    ``documentation`` so the user sees the explanation without extra interaction.
    """
    items: list[CompletionItem] = []
    items.extend(_make_items(SYSTEMRDL_TOP_KEYWORDS, CompletionItemKind.Keyword))
    items.extend(_make_items(SYSTEMRDL_PROPERTIES, CompletionItemKind.Property))
    items.extend(_make_items(SYSTEMRDL_RW_VALUES, CompletionItemKind.EnumMember))
    items.extend(_make_items(SYSTEMRDL_ONWRITE_VALUES, CompletionItemKind.EnumMember))
    items.extend(_make_items(SYSTEMRDL_ONREAD_VALUES, CompletionItemKind.EnumMember))
    items.extend(_make_items(SYSTEMRDL_ADDRESSING_VALUES, CompletionItemKind.EnumMember))
    items.extend(_make_items(SYSTEMRDL_PRECEDENCE_VALUES, CompletionItemKind.EnumMember))
    return items


def _completion_items_for_context(context: str) -> list[CompletionItem]:
    """Return the value subset for a property-RHS context, or [] for general."""
    if context in ("sw_value", "hw_value"):
        return _make_items(SYSTEMRDL_RW_VALUES, CompletionItemKind.EnumMember)
    if context == "onwrite_value":
        return _make_items(SYSTEMRDL_ONWRITE_VALUES, CompletionItemKind.EnumMember)
    if context == "onread_value":
        return _make_items(SYSTEMRDL_ONREAD_VALUES, CompletionItemKind.EnumMember)
    if context == "addressing_value":
        return _make_items(SYSTEMRDL_ADDRESSING_VALUES, CompletionItemKind.EnumMember)
    if context == "precedence_value":
        return _make_items(SYSTEMRDL_PRECEDENCE_VALUES, CompletionItemKind.EnumMember)
    return []


def _user_properties_from_cached(roots: list[RootNode]) -> dict[str, Any]:
    """Pluck user-defined properties out of the first cached root.

    Returns ``{name: PureUserProperty}`` — empty if the file declared none.
    Used by completion (suggest names in property-assignment contexts) and
    hover (explain a user-defined property when the cursor is on its name).
    """
    for r in roots:
        env = getattr(r, "env", None)
        if env is None:
            continue
        rules = getattr(env, "property_rules", None)
        if rules is None:
            continue
        user = getattr(rules, "user_properties", None)
        if user:
            return dict(user)
    return {}


def _completion_items_for_user_properties(roots: list[RootNode]) -> list[CompletionItem]:
    """User-defined properties surface in completion alongside the static catalogue.

    Detail line shows the bindable component class set so the user knows
    where the property is allowed (`field` only, vs. `addrmap+regfile`, etc.).
    """
    items: list[CompletionItem] = []
    for name, prop in _user_properties_from_cached(roots).items():
        bindable = getattr(prop, "bindable_to", None) or set()
        kinds = ", ".join(sorted(c.__name__.lower() for c in bindable)) or "any"
        valid = getattr(prop, "valid_type", None) or "any"
        valid_name = getattr(valid, "__name__", str(valid))
        detail = f"user property — {valid_name} on {kinds}"
        items.append(
            CompletionItem(
                label=name,
                kind=CompletionItemKind.Property,
                detail=detail,
                documentation=(
                    f"User-defined property `{name}` ({valid_name}). "
                    f"Bindable to: {kinds}."
                ),
            )
        )
    return items


def _completion_items_for_types(roots: list[RootNode]) -> list[CompletionItem]:
    """Pull every top-level component definition out of the cached compile."""
    items: list[CompletionItem] = []
    defs = _comp_defs_from_cached(roots)
    for name, comp in defs.items():
        kind_label = type(comp).__name__.lower()
        props = getattr(comp, "properties", {}) or {}
        display_name = props.get("name")
        desc = props.get("desc")
        doc_lines: list[str] = []
        if display_name:
            doc_lines.append(f"**{display_name}**")
        if desc:
            doc_lines.append(str(desc))
        if not doc_lines:
            doc_lines.append(f"User-defined {kind_label} type.")
        items.append(
            CompletionItem(
                label=name,
                kind=CompletionItemKind.Class,
                detail=kind_label,
                documentation="\n\n".join(doc_lines),
            )
        )
    return items
