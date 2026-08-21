from __future__ import annotations

MAX_STATEMENT_CHARACTERS = 500
MAX_PASSAGE_CHARACTERS = 500

_TRUNCATION_MARKER = "\u2026"

_C0_CONTROL = frozenset(
    chr(c) for c in list(range(0x0000, 0x0020)) + [0x007F]
)
_C1_CONTROL = frozenset(chr(c) for c in range(0x0080, 0x00A0))
_INVISIBLE = (
    frozenset(chr(c) for c in range(0x200B, 0x2010))
    | frozenset(chr(c) for c in range(0x2028, 0x202A))
    | frozenset(chr(c) for c in range(0x202A, 0x202F))
    | frozenset(chr(c) for c in range(0x2066, 0x206A))
    | frozenset("\ufeff")
)
_STRIP = (_C0_CONTROL | _C1_CONTROL | _INVISIBLE) - frozenset("\n\t")


def sanitize(value: str) -> str:
    if not isinstance(value, str):
        value = str(value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in value if ch not in _STRIP)


def collapse(value: str) -> str:
    text = sanitize(value)
    return " ".join(text.split())


def bounded(value: str, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    text = sanitize(value)
    if len(text) <= limit:
        return text, False
    return text[: limit - 1].rstrip() + _TRUNCATION_MARKER, True
