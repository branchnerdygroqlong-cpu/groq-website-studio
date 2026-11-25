"""Deterministic CSS fixes for mistakes small models make repeatedly.

These never add design or content. They repair things that are plainly broken:
  * spacing variables defined without a unit (--space-4: 2;), which makes every
    padding/margin/gap that uses them invalid, so the browser silently ignores it
  * visually-hidden utility classes (sr-only etc.) used in the HTML but never defined
"""
from __future__ import annotations

import re

_LENGTH_PROP = re.compile(
    r"^(?:padding|margin|gap|row-gap|column-gap|(?:min-|max-)?(?:width|height)|top|right|"
    r"bottom|left|inset|border-radius|font-size|letter-spacing|outline-offset)(?:-[a-z]+)*$")
_DECL = re.compile(r"([a-zA-Z-]+)\s*:\s*([^;{}]+?)\s*(?=;|\})")
_VARDEF = re.compile(r"(--[\w-]+)(\s*:\s*)(-?\d*\.?\d+)(\s*;)")
_COMMENT = re.compile(r"/\*.*?\*/", re.S)

_VISUALLY_HIDDEN = ("position:absolute;width:1px;height:1px;padding:0;margin:-1px;"
                    "overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0")
_HIDDEN_ALIASES = ("sr-only", "screen-reader-text", "visually-hidden", "visuallyhidden")


def fix_unitless_lengths(css: str):
    """Add 'rem' to bare-number variables that are used as lengths. Returns (css, notes)."""
    plain = _COMMENT.sub("", css)
    bare = {m.group(1): m.group(3) for m in _VARDEF.finditer(plain) if float(m.group(3)) != 0}
    if not bare:
        return css, []
    length_use, other_use, affected = set(), set(), 0
    for match in _DECL.finditer(plain):
        prop, value = match.group(1).lower(), match.group(2)
        hit = False
        for name in re.findall(r"var\(\s*(--[\w-]+)", value):
            if name not in bare:
                continue
            multiplier = re.search(
                rf"[*/]\s*var\(\s*{re.escape(name)}\b|var\(\s*{re.escape(name)}[^)]*\)\s*[*/]", value)
            if _LENGTH_PROP.match(prop) and not multiplier:
                length_use.add(name)
                hit = True
            else:
                other_use.add(name)      # e.g. line-height, a calc() multiplier: leave unitless
        affected += hit
    fix = sorted(length_use - other_use)
    # Spacing-family variables nothing uses yet still need a unit, or the first rule that
    # uses them later (for example a polish round) would be silently invalid.
    unused = [n for n in bare if n not in length_use and n not in other_use
              and re.match(r"^--(?:space|spacing|gap|pad|padding|margin|radius|gutter)\b", n)
              and not re.search(r"scale|ratio|factor|multiplier|lh|line", n)]
    fix = sorted(set(fix) | set(unused))
    if not fix:
        return css, []
    for name in fix:
        css = re.sub(rf"({re.escape(name)}\s*:\s*)(-?\d*\.?\d+)(\s*;)", r"\1\2rem\3", css)
    note = (f"{len(fix)} spacing/size variables had no unit (for example {fix[0]}: {bare[fix[0]]}); "
            f"added rem. About {affected} declarations had been ignored by the browser")
    return css, [note]


def missing_utility_rules(html: str, css: str) -> str:
    """CSS for visually-hidden utility classes the HTML uses but the stylesheet lacks."""
    used = set()
    for match in re.finditer(r"""class\s*=\s*["']([^"']*)["']""", html):
        used.update(match.group(1).split())
    defined = set(re.findall(r"\.([A-Za-z_][\w-]*)", _COMMENT.sub("", css)))
    return "\n".join(f".{name}{{{_VISUALLY_HIDDEN}}}"
                     for name in _HIDDEN_ALIASES if name in used and name not in defined)
