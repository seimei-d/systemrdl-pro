"""textDocument/completion: keyword + property + value catalogues + user-defined types.

Static catalogue: label → one-line markdown shown in the completion popup's
detail panel. Coverage is intentionally narrower than the full SystemRDL 2.0
spec — we cover the properties and access modes that matter for ~95% of real
register definitions.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from lsprotocol.types import CompletionItem, CompletionItemKind, InsertTextFormat

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

# Boolean properties — `prop = true | false`.
SYSTEMRDL_BOOL_VALUES: dict[str, str] = {
    "true":  "Boolean true.",
    "false": "Boolean false.",
}


def _build_property_metadata() -> tuple[
    dict[str, dict[str, str]],          # prop → value catalogue
    dict[str, set[str]],                # component-class-name → dyn-allowed prop names
    dict[str, set[str]],                # component-class-name → all bindable prop names
]:
    """Mine systemrdl-compiler's authoritative property registry.

    Returns three maps used by RHS / `->` completion:

    1. ``prop → catalogue`` — what literal values to suggest after ``prop = ``.
       Built from each rule's ``valid_types`` so adding a new SystemRDL
       property type to the compiler propagates here automatically.
    2. ``component → dyn-allowed`` — only these are valid behind ``inst->``.
       Mirrors the SystemRDL spec's "dynamic assignment" column (Table 11+).
    3. ``component → all-bindable`` — for tooling that wants the full set
       (we don't currently use it but keep the bookkeeping cheap).

    Module-level so we pay the import + walk cost once.
    """
    import systemrdl.component as _comp
    from systemrdl import RDLCompiler
    from systemrdl.rdltypes import (
        AccessType,
        AddressingType,
        InterruptType,
        OnReadType,
        OnWriteType,
        PrecedenceType,
    )

    enum_catalogues: dict[type, dict[str, str]] = {}
    for et, doc_prefix in [
        (AccessType,     "Access mode"),
        (OnReadType,     "On-read action"),
        (OnWriteType,    "On-write action"),
        (PrecedenceType, "Precedence"),
        (AddressingType, "Addressing mode"),
        (InterruptType,  "Interrupt edge type"),
    ]:
        enum_catalogues[et] = {m.name: f"{doc_prefix}: {m.name}." for m in et}
    # ``ro``/``wo`` are SystemRDL spec aliases not surfaced by the compiler
    # enum (which uses ``r``/``w``); add them so completion matches the
    # spelling most users actually type.
    enum_catalogues[AccessType]["ro"] = "Read-only (alias of `r`)."
    enum_catalogues[AccessType]["wo"] = "Write-only (alias of `w`)."

    rdl = RDLCompiler()
    rules = rdl.env.property_rules.rdl_properties

    # Filter out names with spaces — they're internal compiler quirks
    # (e.g. ``"intr type"``) and would break the regex / popup labels.
    rules = {name: rule for name, rule in rules.items() if " " not in name}

    prop_to_cat: dict[str, dict[str, str]] = {}
    for name, rule in rules.items():
        cat: dict[str, str] = {}
        for vt in rule.valid_types:
            if not isinstance(vt, type):
                # ArrayedType / parameterised types — no closed-set of literals.
                continue
            if vt is bool:
                cat.update(SYSTEMRDL_BOOL_VALUES)
            elif vt in enum_catalogues:
                cat.update(enum_catalogues[vt])
            # int / str / Signal / Field / PropertyReference → no closed-set
            # of legal literals; skip — user types the expression freehand.
        if cat:
            prop_to_cat[name] = cat

    classes = {
        "Field":   _comp.Field,
        "Reg":     _comp.Reg,
        "Regfile": _comp.Regfile,
        "Addrmap": _comp.Addrmap,
        "Mem":     _comp.Mem,
        "Signal":  _comp.Signal,
    }
    dyn_allowed: dict[str, set[str]] = {cn: set() for cn in classes}
    all_bindable: dict[str, set[str]] = {cn: set() for cn in classes}
    for name, rule in rules.items():
        for cn, cl in classes.items():
            if cl in rule.bindable_to:
                all_bindable[cn].add(name)
                if rule.dyn_assign_allowed:
                    dyn_allowed[cn].add(name)
    return prop_to_cat, dyn_allowed, all_bindable


_PROP_VALUE_CATALOGUES, _DYN_PROPS_BY_CLASS, _ALL_PROPS_BY_CLASS = _build_property_metadata()


# Property docstrings. The bulk catalogue (`SYSTEMRDL_PROPERTIES` above)
# already has the per-name explanation; we reuse it for `->` popup detail.
def _prop_doc(name: str) -> str:
    return SYSTEMRDL_PROPERTIES.get(name, "SystemRDL property.")


# Build the assignment regex from every property the compiler knows about
# (not just the ones with closed value sets — the user might type
# ``regwidth = 32`` and want VSCode to recognise the context even though
# we have nothing to suggest). Sort longest-first so e.g. `incrsaturate`
# wins over `saturate`.
_ALL_PROP_NAMES = {n for cls in _ALL_PROPS_BY_CLASS.values() for n in cls}
_COMPLETION_CONTEXT_RE = re.compile(
    r"\b(" + "|".join(sorted(_ALL_PROP_NAMES, key=len, reverse=True))
    + r")\s*=\s*\w*$"
)
# Member access — `WIDE_REG.<cursor>` or `top.WIDE_REG.<cursor>`. The
# trailing ``\w*`` lets the popup stay open while the user types a
# partial member name.
_MEMBER_ACCESS_RE = re.compile(
    r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.(\w*)$"
)
# Property access — `WIDE_REG->reset` etc. SystemRDL clause 12 dynamic
# property assignment.
_PROPERTY_ACCESS_RE = re.compile(
    r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*->\s*(\w*)$"
)


def _completion_context(text: str, line_0b: int, char_0b: int) -> str:
    """Detect what the cursor is right of, so we can narrow the suggestion list.

    Returns:
        - ``"value:<prop>"`` for property RHS contexts (``rclr = `` →
          ``"value:rclr"``). The caller maps the property name to its
          legal-value catalogue.
        - ``"member:<dotted.path>"`` after ``WIDE_REG.`` / ``top.CTRL.``.
        - ``"property:<dotted.path>"`` after ``WIDE_REG->`` / ``CTRL.enable->``.
        - ``"general"`` otherwise.

    Single-line match — SystemRDL accessors rarely span lines.
    """
    lines = text.splitlines()
    if line_0b < 0 or line_0b >= len(lines):
        return "general"
    prefix = lines[line_0b][:char_0b]
    m = _COMPLETION_CONTEXT_RE.search(prefix)
    if m:
        return f"value:{m.group(1)}"
    # Property access checked BEFORE member: `a->b` starts with `a-` so the
    # member regex's `\.` wouldn't fire, but explicit ordering keeps intent
    # clear if either pattern grows.
    pm = _PROPERTY_ACCESS_RE.search(prefix)
    if pm:
        return f"property:{pm.group(1)}"
    mm = _MEMBER_ACCESS_RE.search(prefix)
    if mm:
        return f"member:{mm.group(1)}"
    return "general"


def _make_items(catalogue: dict[str, str], kind: CompletionItemKind) -> list[CompletionItem]:
    return [
        CompletionItem(label=label, kind=kind, detail=doc, documentation=doc)
        for label, doc in catalogue.items()
    ]


# Snippet bodies for top-level keywords. Cursor stops at $1, $2, ... and
# lands at $0 when finished. Plain-string completions (everything not in
# this map) just insert the keyword as-is.
# Snippet bodies — line-length lints don't help here; prose-wrap would
# break the literal `\n` markers VSCode interprets.
_KEYWORD_SNIPPETS: dict[str, str] = {
    "addrmap":  "addrmap ${1:name} {\n\t$0\n};",
    "regfile":  "regfile ${1:type_name} {\n\t$0\n} ${2:inst_name} @ ${3:0x0};",
    "reg":      "reg {\n\tfield { sw=${1|rw,ro,wo|}; hw=${2|r,w,rw|}; } ${3:name}[${4:0}:${5:0}] = ${6:0};\n} ${7:NAME} @ ${8:0x0};",  # noqa: E501
    "field":    "field { sw=${1|rw,ro,wo|}; hw=${2|r,w,rw|}; } ${3:name}[${4:0}:${5:0}] = ${6:0};",
    "enum":     "enum ${1:Name} {\n\t${2:VALUE_A} = ${3:0};\n\t$0\n};",
    "mem":      "mem ${1:type_name} {\n\tmementries = ${2:1024};\n\tmemwidth = ${3:32};\n} ${4:inst_name} @ ${5:0x0};",  # noqa: E501
    "signal":   "signal { activehigh=true; } ${1:name};",
    "property": "property ${1:name} {\n\ttype = ${2:boolean};\n\tcomponent = ${3:field};\n\tdefault = ${4:false};\n};",  # noqa: E501
}


def _make_keyword_items() -> list[CompletionItem]:
    items: list[CompletionItem] = []
    for label, doc in SYSTEMRDL_TOP_KEYWORDS.items():
        snippet = _KEYWORD_SNIPPETS.get(label)
        if snippet is not None:
            items.append(CompletionItem(
                label=label, kind=CompletionItemKind.Keyword,
                detail=doc, documentation=doc,
                insert_text=snippet,
                insert_text_format=InsertTextFormat.Snippet,
            ))
        else:
            items.append(CompletionItem(
                label=label, kind=CompletionItemKind.Keyword,
                detail=doc, documentation=doc,
            ))
    return items


def _completion_items_static() -> list[CompletionItem]:
    """Full keyword + property + value catalogue with one-line docs.

    Used for the ``"general"`` context. Each item carries both ``detail`` and
    ``documentation`` so the user sees the explanation without extra interaction.
    """
    items: list[CompletionItem] = []
    items.extend(_make_keyword_items())
    items.extend(_make_items(SYSTEMRDL_PROPERTIES, CompletionItemKind.Property))
    items.extend(_make_items(SYSTEMRDL_RW_VALUES, CompletionItemKind.EnumMember))
    items.extend(_make_items(SYSTEMRDL_ONWRITE_VALUES, CompletionItemKind.EnumMember))
    items.extend(_make_items(SYSTEMRDL_ONREAD_VALUES, CompletionItemKind.EnumMember))
    items.extend(_make_items(SYSTEMRDL_ADDRESSING_VALUES, CompletionItemKind.EnumMember))
    items.extend(_make_items(SYSTEMRDL_PRECEDENCE_VALUES, CompletionItemKind.EnumMember))
    return items


def _completion_items_for_context(context: str) -> list[CompletionItem]:
    """Return the value subset for a property-RHS context, or [] for general."""
    if context.startswith("value:"):
        prop = context[len("value:"):]
        cat = _PROP_VALUE_CATALOGUES.get(prop)
        if cat:
            return _make_items(cat, CompletionItemKind.EnumMember)
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


def _enclosing_instance_scope(text: str, line_0b: int, char_0b: int) -> str | None:
    """Best-effort: dotted instance path of the cursor's enclosing addrmap.

    Walks backward through balanced ``{`` / ``}`` (strings + comments
    stripped to whitespace so braces inside literals don't confuse us) and
    looks at the token immediately before each enclosing ``{``. When that
    token is the INSTANCE name of an addrmap or regfile (e.g. the ``top``
    in ``addrmap top { … }``, NOT the type ``dma_channel_t`` in
    ``regfile dma_channel_t { … }``), it joins the chain into a dotted
    prefix the caller can use to scope instance suggestions.

    Returns ``None`` when no enclosing instance is detectable — completion
    falls back to the unscoped global list.
    """
    lines = text.splitlines()
    if line_0b < 0 or line_0b >= len(lines):
        return None

    # Cursor offset into the full text.
    cursor_off = sum(len(L) + 1 for L in lines[:line_0b]) + char_0b
    if cursor_off > len(text):
        cursor_off = len(text)

    # Strip strings + comments so braces inside them don't unbalance the scan.
    cleaned = re.sub(r'"(?:\\.|[^"\\])*"', lambda m: " " * len(m.group(0)), text)
    cleaned = re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)), cleaned)
    cleaned = re.sub(
        r"/\*[\s\S]*?\*/",
        lambda m: re.sub(r"[^\n]", " ", m.group(0)),
        cleaned,
    )

    # Walk backward from cursor, collecting the token preceding each `{`
    # that the cursor is inside. We only care about INSTANCE-style scopes;
    # type definitions (``regfile dma_channel_t {``) intentionally fall
    # through, since the body is the type's template — completing instance
    # names there is misleading.
    chain: list[str] = []
    depth = 0
    i = cursor_off - 1
    while i >= 0:
        c = cleaned[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                # Find the token (and its preceding kind keyword) that opens
                # this brace. Look back skipping whitespace.
                j = i - 1
                while j >= 0 and cleaned[j].isspace():
                    j -= 1
                # Read a backwards identifier.
                end = j + 1
                while j >= 0 and (cleaned[j].isalnum() or cleaned[j] == "_"):
                    j -= 1
                ident = cleaned[j + 1:end]
                # Skip back over whitespace to look for `addrmap`/`regfile`/etc.
                k = j
                while k >= 0 and cleaned[k].isspace():
                    k -= 1
                kw_end = k + 1
                while k >= 0 and (cleaned[k].isalnum() or cleaned[k] == "_"):
                    k -= 1
                kw = cleaned[k + 1:kw_end]
                if kw in ("addrmap", "regfile", "reg", "field", "mem"):
                    # Type definition — `<kw> TYPE_NAME { ... }`. Skip; type
                    # bodies define a template, instance refs aren't valid here.
                    pass
                elif ident:
                    # Bare identifier before `{` — this is an instance scope
                    # like `top { ... }` (post-instantiation form) or, more
                    # commonly, an inline body. Treat as instance.
                    chain.append(ident)
                # Anonymous / unrecognised → don't add a scope segment but
                # keep walking outward.
                depth = 0
            else:
                depth -= 1
        i -= 1

    if not chain:
        return None
    chain.reverse()
    return ".".join(chain)


def _resolve_node_by_path(roots: list[RootNode], dotted: str) -> Any | None:
    """Resolve ``a.b.c`` against the elaborated tree, returning the node.

    Tries two interpretations: (1) absolute from a root (``top.CTRL.enable``),
    (2) suffix-match where the last unique instance with that bare name is
    picked (``WIDE_REG`` works without typing ``top.``). Returns ``None``
    if ambiguous-no-match.
    """
    from systemrdl.node import RegNode

    segs = [s for s in dotted.split(".") if s]
    if not segs:
        return None

    def descend(node: Any, names: list[str]) -> Any | None:
        cur = node
        for nm in names:
            nxt = None
            try:
                for child in cur.children(unroll=False, skip_not_present=False):
                    if getattr(child, "inst_name", None) == nm:
                        nxt = child
                        break
                if nxt is None and isinstance(cur, RegNode):
                    for f in cur.fields(skip_not_present=False):
                        if getattr(f, "inst_name", None) == nm:
                            nxt = f
                            break
            except Exception:
                return None
            if nxt is None:
                return None
            cur = nxt
        return cur

    # Absolute from a root.
    for r in roots:
        try:
            for c in r.children(unroll=False, skip_not_present=False):
                if getattr(c, "inst_name", None) == segs[0]:
                    found = descend(c, segs[1:])
                    if found is not None:
                        return found
        except Exception:
            continue

    # Suffix match: walk every node, return the unique one whose inst_name
    # equals the LAST segment AND whose ancestor names match the leading
    # segments. ``children()`` on a RegNode already yields fields, so the
    # walk recurses uniformly without a separate fields() pass.
    matches: list[Any] = []

    def walk(node: Any, ancestry: list[str]) -> None:
        try:
            kids = list(node.children(unroll=False, skip_not_present=False))
        except Exception:
            kids = []
        for c in kids:
            nm = getattr(c, "inst_name", None) or ""
            new_anc = [*ancestry, nm]
            if nm == segs[-1]:
                tail = new_anc[-len(segs):] if len(segs) <= len(new_anc) else None
                if tail == segs:
                    matches.append(c)
            walk(c, new_anc)

    for r in roots:
        walk(r, [])

    # Dedupe by node identity — two walk paths can hit the same node when
    # children()/fields() overlap on some compiler versions.
    unique: list[Any] = []
    seen_ids: set[int] = set()
    for m in matches:
        mid = id(m)
        if mid in seen_ids:
            continue
        seen_ids.add(mid)
        unique.append(m)
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        # Ambiguous bare name (e.g. ``DMA_BASE_ADDR`` instantiated under 4
        # channels). When every match is an instance of the same type the
        # structure is identical, so for completion purposes any one of
        # them works — return the first. Distinct types stay ambiguous.
        type_names = set()
        for m in unique:
            inst = getattr(m, "inst", None)
            tn = getattr(inst, "type_name", None) if inst is not None else None
            type_names.add(tn or type(m).__name__)
        if len(type_names) == 1:
            return unique[0]
    return None


_FIELD_BUILTIN_REFS: dict[str, str] = {
    # Reduction operators (SystemRDL 2.0 §11.1.2): bitwise reductions of
    # all bits in the field. Most commonly used to wire status outputs.
    "anded": "Bitwise AND of all field bits.",
    "ored":  "Bitwise OR of all field bits.",
    "xored": "Bitwise XOR (parity) of all field bits.",
    # Interrupt-field references (§9.7).
    "intr": "Interrupt-pending output (only on `intr` fields).",
    "halt": "Halt-pending output (only on `halt`-style intr fields).",
}


def _completion_items_for_members(roots: list[RootNode], dotted: str) -> list[CompletionItem]:
    """Children of the node at ``dotted``. For fields: built-in references
    (``.anded``/``.ored``/``.xored``/``.intr``/``.halt``). Empty if path
    didn't resolve.

    ``RegNode.children()`` already yields fields, so a single walk covers
    both addressable children and field leaves of regs.
    """
    from systemrdl.node import (
        FieldNode,
        RegfileNode,
        RegNode,
    )

    node = _resolve_node_by_path(roots, dotted)
    if node is None:
        return []
    if isinstance(node, FieldNode):
        # Field is a leaf in the instance hierarchy, but `.<builtin>` is
        # still valid syntax — surface the reduction + intr references.
        items_field = _make_items(_FIELD_BUILTIN_REFS, CompletionItemKind.Property)
        for it in items_field:
            it.detail = "field reference"
        return items_field
    items: list[CompletionItem] = []

    def kind_for(c: Any) -> CompletionItemKind:
        if isinstance(c, FieldNode):
            return CompletionItemKind.Field
        if isinstance(c, RegNode):
            return CompletionItemKind.Variable
        if isinstance(c, RegfileNode):
            return CompletionItemKind.Module
        return CompletionItemKind.Class

    seen: set[str] = set()
    try:
        for c in node.children(unroll=False, skip_not_present=False):
            nm = getattr(c, "inst_name", None)
            if not nm or nm in seen:
                continue
            seen.add(nm)
            type_label = type(c).__name__.replace("Node", "").lower()
            bits = ""
            if isinstance(c, FieldNode):
                try:
                    bits = f" [{c.msb}:{c.lsb}]"
                except Exception:
                    pass
            items.append(
                CompletionItem(
                    label=nm,
                    kind=kind_for(c),
                    detail=f"{type_label}{bits}",
                    documentation=f"`{dotted}.{nm}` ({type_label}{bits})",
                    filter_text=nm,
                )
            )
    except Exception:
        pass
    return items


def _completion_items_for_properties_of(
    roots: list[RootNode], dotted: str,
) -> list[CompletionItem]:
    """SystemRDL properties **valid behind ``->``** for the node at ``dotted``.

    Filtered through systemrdl-compiler's ``dyn_assign_allowed`` flag —
    properties like ``regwidth`` / ``accesswidth`` / ``shared`` / ``bridge``
    cannot be assigned dynamically (they're structural / locked at
    elaboration time), so they're omitted from the popup. Otherwise the
    user gets misleading suggestions that the compiler will reject.
    """
    from systemrdl.node import (
        AddrmapNode,
        FieldNode,
        MemNode,
        RegfileNode,
        RegNode,
        SignalNode,
    )

    node = _resolve_node_by_path(roots, dotted)
    if node is None:
        return []
    cls_lookup = [
        (FieldNode,   "Field"),
        (RegNode,     "Reg"),
        (RegfileNode, "Regfile"),
        (AddrmapNode, "Addrmap"),
        (MemNode,     "Mem"),
        (SignalNode,  "Signal"),
    ]
    cls_name = next((cn for nc, cn in cls_lookup if isinstance(node, nc)), None)
    if cls_name is None:
        return []
    allowed = _DYN_PROPS_BY_CLASS.get(cls_name, set())
    catalogue = {n: _prop_doc(n) for n in sorted(allowed)}
    return _make_items(catalogue, CompletionItemKind.Property)


def _completion_items_for_instances(
    roots: list[RootNode], scope_prefix: str | None = None,
) -> list[CompletionItem]:
    """Walk the elaborated tree, surface reg/field/container instance names.

    Dedupe by short name: when the same name (e.g. ``DMA_BASE_ADDR``) is
    instantiated multiple times via a shared regfile type, the popup gets
    one entry whose detail shows the instance count and a sample path.
    Documentation lists every full path so the user can disambiguate.

    ``scope_prefix`` (if provided) restricts the walk to instances whose
    full dotted path begins with that prefix — used by the editor-side
    scope filter (``addrmap top { … cursor … }`` only suggests names
    actually visible in ``top.``).
    """
    from systemrdl.node import (
        AddrmapNode,
        FieldNode,
        MemNode,
        RegfileNode,
        RegNode,
    )

    def kind_for(node: Any) -> CompletionItemKind:
        if isinstance(node, FieldNode):
            return CompletionItemKind.Field
        if isinstance(node, RegNode):
            return CompletionItemKind.Variable
        if isinstance(node, RegfileNode):
            return CompletionItemKind.Module
        if isinstance(node, (AddrmapNode, MemNode)):
            return CompletionItemKind.Class
        return CompletionItemKind.Reference

    # name → (kind, type_label, [paths], first_addr_str)
    grouped: dict[str, dict[str, Any]] = {}

    def remember(name: str, node: Any, path: str) -> None:
        type_label = type(node).__name__.replace("Node", "").lower()
        try:
            addr = getattr(node, "absolute_address", None)
            addr_str = f" @ 0x{addr:x}" if isinstance(addr, int) else ""
        except Exception:
            addr_str = ""
        entry = grouped.get(name)
        if entry is None:
            grouped[name] = {
                "kind": kind_for(node),
                "type_label": type_label,
                "paths": [path],
                "addr_str": addr_str,
            }
        else:
            entry["paths"].append(path)

    def walk(node: Any, segs: list[str]) -> None:
        for child in node.children(unroll=False, skip_not_present=False):
            name = getattr(child, "inst_name", None)
            if not name:
                continue
            path = ".".join([*segs, name])
            if scope_prefix is None or path.startswith(scope_prefix):
                remember(name, child, path)
            if isinstance(child, (AddrmapNode, RegfileNode, MemNode)):
                walk(child, [*segs, name])
            elif isinstance(child, RegNode):
                # Surface field names too — `field.bitfield` references appear
                # in counter / dynamic property contexts.
                for f in child.fields(skip_not_present=False):
                    fname = getattr(f, "inst_name", None)
                    if not fname:
                        continue
                    fpath = ".".join([*segs, name, fname])
                    if scope_prefix is None or fpath.startswith(scope_prefix):
                        remember(fname, f, fpath)

    for r in roots:
        try:
            walk(r, [])
        except Exception:
            # systemrdl-compiler can raise on traversal of partially-constructed
            # trees during a stale-cache window. Skip the root rather than crash
            # the whole completion request.
            continue

    items: list[CompletionItem] = []
    for name, entry in grouped.items():
        paths: list[str] = entry["paths"]
        count = len(paths)
        first_path = paths[0]
        if count == 1:
            detail = f"{entry['type_label']}{entry['addr_str']} · {first_path}"
            doc = f"`{first_path}` ({entry['type_label']})"
        else:
            detail = f"{entry['type_label']} · {count} instances"
            # List up to first 8 paths in docs so big designs don't dump 25k
            # lines into the side panel; a one-liner footer flags the rest.
            shown = paths[:8]
            extra = "" if count <= 8 else f"\n…and {count - 8} more"
            doc = (
                f"{count} instances of `{name}` ({entry['type_label']}):\n"
                + "\n".join(f"- `{p}`" for p in shown)
                + extra
            )
        items.append(
            CompletionItem(
                label=name,
                kind=entry["kind"],
                detail=detail,
                documentation=doc,
                filter_text=name,
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
        # Unwrap AST literals — `comp.properties` values are systemrdl
        # `StringLiteral`s, not raw strings, and printing them directly
        # leaks Python repr.
        raw_name = props.get("name")
        raw_desc = props.get("desc")
        display_name = (
            raw_name.get_value() if hasattr(raw_name, "get_value") else raw_name
        ) if raw_name is not None else None
        desc = (
            raw_desc.get_value() if hasattr(raw_desc, "get_value") else raw_desc
        ) if raw_desc is not None else None
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
