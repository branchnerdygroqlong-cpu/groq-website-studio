#!/usr/bin/env python3
"""Edit the pages of a generated Studio site with plain forms. No AI, standard library only.

What it does
  * lists the editable parts of a page (headings, paragraphs, links, pictures, button text,
    form labels ...) grouped by section, so the Studio can show one box for each
  * writes changed boxes back into the page's HTML at the exact spot they came from, so
    everything you did not touch stays byte-for-byte the same
  * adds, moves, duplicates and deletes sections; adds and deletes whole pages
  * keeps the menu and footer the same on every page (they are edited on the Home page and
    copied to the others, the same way shop_cart.py copies them)
  * keeps the previous version of every page it changes in <site>/.edit_history (Undo)

Pages made by the shop (shop.html, cart.html) are rebuilt by shop_cart.py, so they are not
editable here. Contact and social links are managed by site_extras.py and are left alone.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

HISTORY_DIR = ".edit_history"
HISTORY_KEEP = 40
GENERATED = {"shop.html", "cart.html"}
PROTECTED = {"index.html", "shop.html", "cart.html"}
FILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}\.html$")
MAX_TEXT = 8000
CSS_MARKER = "/* STUDIO-SECTIONS */"

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr"}
SKIP = {"script", "style", "noscript", "template", "svg", "head", "link", "meta", "title"}
SKIP_SEQ = {"script", "style", "noscript", "template", "link", "meta", "br"}
BLOCKISH = {"address", "article", "aside", "blockquote", "div", "dl", "fieldset", "figure",
            "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main", "nav",
            "ol", "p", "pre", "section", "table", "ul"}
TEXT_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "a", "button", "label", "figcaption",
             "blockquote", "dt", "dd", "th", "td", "summary", "caption"}
INLINE = {"a", "abbr", "b", "bdi", "br", "cite", "code", "data", "dfn", "em", "i", "kbd", "mark",
          "q", "s", "samp", "small", "span", "strong", "sub", "sup", "time", "u", "var", "wbr"}
LABELS = {"h1": "Main heading", "h2": "Heading", "h3": "Sub-heading", "h4": "Sub-heading",
          "h5": "Sub-heading", "h6": "Sub-heading", "p": "Paragraph", "li": "List item",
          "a": "Link text", "button": "Button text", "label": "Form label",
          "figcaption": "Picture caption", "blockquote": "Quote", "dt": "Term",
          "dd": "Description", "th": "Table heading", "td": "Table cell", "summary": "Summary",
          "caption": "Table caption"}
REMOVABLE = {"p", "li", "figcaption", "blockquote", "dt", "dd"}
MULTILINE = {"p", "blockquote", "dd", "figcaption"}
_ID_RE = re.compile(r"(\d+):(text|html|href|src|alt|ph|remove)")
_ATTR_ID = re.compile(r"""\sid\s*=\s*("[^"]*"|'[^']*')""", re.I)


class EditError(Exception):
    """Something the person typed is not acceptable. `fields` maps a box id to a message."""
    def __init__(self, message, fields=None):
        super().__init__(message)
        self.fields = fields or {}


class Conflict(Exception):
    """The page on disk is not the version the form was built from."""


# --------------------------------------------------------------------------- #
# A small HTML tree that remembers where every element sits in the source text
# --------------------------------------------------------------------------- #
class Node:
    __slots__ = ("tag", "attrs", "start", "open_end", "close_start", "close_end", "parent", "children")

    def __init__(self, tag, attrs, start, open_end, parent=None):
        self.tag, self.attrs, self.start, self.open_end = tag, attrs, start, open_end
        self.close_start = self.close_end = None
        self.parent, self.children = parent, []

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def cls(self):
        return (self.attrs.get("class") or "").split()


class Doc:
    def __init__(self, src, root, by_start):
        self.src, self.root, self.by_start = src, root, by_start

    def find(self, tag):
        return next((n for n in self.root.walk() if n.tag == tag), None)


class _Builder(HTMLParser):
    def __init__(self, src):
        super().__init__(convert_charrefs=False)
        self.src = src
        self.starts = [0] + [m.end() for m in re.finditer("\n", src)]
        self.root = Node("#root", {}, 0, 0)
        self.root.close_start = self.root.close_end = len(src)
        self.stack, self.by_start = [self.root], {}

    def _off(self):
        line, col = self.getpos()
        return self.starts[line - 1] + col

    def _close_to(self, index, pos):
        while len(self.stack) > index:
            node = self.stack.pop()
            node.close_start = node.close_end = pos

    def _implicit(self, tag, pos):
        if self.stack[-1].tag == "p" and tag in BLOCKISH:
            self._close_to(len(self.stack) - 1, pos)
        if tag == "li":
            for i in range(len(self.stack) - 1, 0, -1):
                name = self.stack[i].tag
                if name == "li":
                    self._close_to(i, pos)
                    break
                if name in ("ul", "ol"):
                    break

    def _make(self, tag, attrs, void):
        start = self._off()
        raw = self.get_starttag_text() or ""
        self._implicit(tag, start)
        node = Node(tag, {k: (v or "") for k, v in attrs}, start, start + len(raw), self.stack[-1])
        self.stack[-1].children.append(node)
        self.by_start[start] = node
        if void or tag in VOID:
            node.close_start = node.close_end = node.open_end
        else:
            self.stack.append(node)

    def handle_starttag(self, tag, attrs):
        self._make(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._make(tag, attrs, True)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        start = self._off()
        gt = self.src.find(">", start)
        end = gt + 1 if gt >= 0 else len(self.src)
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self._close_to(i + 1, start)
                node = self.stack.pop()
                node.close_start, node.close_end = start, end
                return


def parse(src: str) -> Doc:
    builder = _Builder(src)
    builder.feed(src)
    builder.close()
    builder._close_to(1, len(src))
    return Doc(src, builder.root, builder.by_start)


# --------------------------------------------------------------------------- #
# Reading text out of the source and writing it back
# --------------------------------------------------------------------------- #
def display_text(raw: str) -> str:
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    raw = re.sub(r"\s+", " ", raw)
    raw = re.sub(r"\s*<br\s*/?>\s*", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", "", raw)
    return html.unescape(raw).strip()


def _norm_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(" ".join(line.split()) for line in value.split("\n")).strip()


def _encode_text(value: str) -> str:
    return html.escape(value, quote=False).replace("\n", "<br>")


def own_text(src: str, node: Node) -> str:
    if node.close_start is None:
        return ""
    pos, parts = node.open_end, []
    for child in node.children:
        parts.append(src[pos:child.start])
        pos = max(pos, child.close_end if child.close_end is not None else child.open_end)
    parts.append(src[pos:node.close_start])
    return re.sub(r"<!--.*?-->", "", "".join(parts), flags=re.S).strip()


def _has(node: Node, tags) -> bool:
    return any(n.tag in tags for n in node.walk())


def set_attr(tag_text: str, name: str, value: str) -> str:
    escaped = html.escape(value, quote=True)
    pattern = re.compile(r"""(\s%s\s*=\s*)("[^"]*"|'[^']*'|[^\s>]+)""" % re.escape(name), re.I)
    if pattern.search(tag_text):
        return pattern.sub(lambda m: f'{m.group(1)}"{escaped}"', tag_text, count=1)
    end = re.search(r"\s*/?>$", tag_text)
    cut = end.start() if end else len(tag_text)
    return tag_text[:cut] + f' {name}="{escaped}"' + tag_text[cut:]


def _check_html(value: str) -> None:
    if (re.search(r"<\s*/?\s*(script|iframe|object|embed|style|link|meta|form|input|textarea|select|svg|base)\b",
                  value, re.I)
            or re.search(r"""[\s"']on\w+\s*=""", value, re.I) or re.search(r"javascript\s*:", value, re.I)):
        raise ValueError("Scripts and embedded code are not allowed here.")

    problems, stack = [], []

    class Check(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag not in VOID:
                stack.append(tag)

        def handle_endtag(self, tag):
            if stack and stack[-1] == tag:
                stack.pop()
            else:
                problems.append(tag)

    checker = Check()
    checker.feed(value)
    checker.close()
    if problems or stack:
        raise ValueError("Every tag needs a matching closing tag, for example <strong>text</strong>.")


def _check_href(value: str) -> None:
    if not value:
        raise ValueError("Enter where the link goes.")
    if len(value) > 2000 or re.search(r"[\x00-\x20]", value):
        raise ValueError("A link address cannot contain spaces.")
    if re.match(r"^[a-z][a-z0-9+.-]*:", value, re.I) and not re.match(r"^(https?|mailto|tel):", value, re.I):
        raise ValueError("Only web links, mailto: and tel: links are allowed.")
    if value.startswith("//"):
        raise ValueError("Start web links with https://")


def _check_src(value: str) -> None:
    if not value:
        raise ValueError("Choose a picture.")
    if len(value) > 2000 or re.search(r"[\x00-\x20]", value):
        raise ValueError("A picture address cannot contain spaces.")
    if re.match(r"^https?://", value, re.I):
        return
    if re.match(r"^[a-z][a-z0-9+.-]*:", value, re.I) or value.startswith(("/", "\\")) or ".." in value.split("/"):
        raise ValueError("Use a picture from this site's folder, or a full https:// address.")


# --------------------------------------------------------------------------- #
# The outline: which boxes to show for a page
# --------------------------------------------------------------------------- #
def _field(node, kind, label, value, **extra):
    data = {"id": f"{node.start}:{kind}", "kind": kind, "tag": node.tag, "label": label,
            "value": value, "group": node.start, "remove": None, "multiline": False}
    data.update(extra)
    return data


def _content_fields(src, node, out):
    kids = node.children
    if node.close_start is None:
        return
    label = LABELS.get(node.tag, "Text")
    raw = src[node.open_end:node.close_start]
    if not kids or all(c.tag == "br" for c in kids):
        value = display_text(raw)
        out.append(_field(node, "text", label, value, multiline=node.tag in MULTILINE or "\n" in value))
        return
    if own_text(src, node) and all(c.tag in INLINE and not _has(c, {"img", "input"}) for c in kids):
        value = " ".join(re.sub(r"<!--.*?-->", "", raw, flags=re.S).split())
        out.append(_field(node, "html", label + " (with formatting)", value, multiline=True))
        return
    for child in kids:
        collect(src, child, out)


def collect(src, node, out):
    if node.tag in SKIP:
        return
    if "data-shop-sync" in node.attrs or "data-shop-link" in node.attrs or "site-extras" in node.cls():
        return
    tag = node.tag
    if tag == "img":
        out.append(_field(node, "src", "Picture", node.attrs.get("src", ""), preview=True))
        out.append(_field(node, "alt", "Picture description (alt text)", node.attrs.get("alt", "")))
    elif tag in ("input", "textarea"):
        if tag == "input" and node.attrs.get("type", "text").lower() not in ("text", "email", "search", "tel", "url", ""):
            return
        out.append(_field(node, "ph", "Placeholder text", node.attrs.get("placeholder", "")))
    elif tag == "a":
        _content_fields(src, node, out)
        if "href" in node.attrs:
            out.append(_field(node, "href", "Link goes to", node.attrs["href"], link=True))
    elif tag in TEXT_TAGS:
        _content_fields(src, node, out)
    else:
        for child in node.children:
            collect(src, child, out)


def _remove_target(src, node):
    if node.tag in REMOVABLE or node.tag == "img":
        return node
    parent = node.parent
    if node.tag == "a" and parent is not None and parent.tag == "li" and len(parent.children) == 1 \
            and not own_text(src, parent):
        return parent
    return None


def block_fields(doc, node):
    fields = []
    collect(doc.src, node, fields)
    seen = set()
    for f in fields:                       # one Remove button per element, on its first box
        if f["group"] in seen:
            continue
        seen.add(f["group"])
        target = _remove_target(doc.src, doc.by_start[f["group"]])
        if target is not None:
            f["remove"] = f"{target.start}:remove"
    return fields


def _pretty(name: str) -> str:
    return re.sub(r"[-_]+", " ", name).strip().capitalize()


def _label(src, node):
    for n in node.walk():
        if n.tag in ("h1", "h2", "h3", "h4") and n.close_start is not None:
            text = display_text(src[n.open_end:n.close_start]).replace("\n", " ")
            if text:
                return text[:60]
    if node.attrs.get("id"):
        return _pretty(node.attrs["id"])
    return f"<{node.tag}> block"


def _structure(doc):
    body = doc.find("body") or doc.root
    main = next((c for c in body.children if c.tag == "main"), None) or next(
        (n for n in body.walk() if n.tag == "main"), None)
    header = next((c for c in body.children if c.tag == "header"), None)
    footer = next((c for c in reversed(body.children) if c.tag == "footer"), None)
    parent = main or body
    seq = [c for c in parent.children if c.tag not in SKIP_SEQ]
    return body, main, header, footer, parent, seq


def _title_node(doc):
    return doc.find("title")


def _description_node(doc):
    return next((n for n in doc.root.walk() if n.tag == "meta"
                 and n.attrs.get("name", "").lower() == "description"), None)


def build_outline(src: str, file: str, editable_shared: bool) -> dict:
    doc = parse(src)
    body, main, header, footer, parent, seq = _structure(doc)

    def record(node, i, role, up, down):
        pinned = role is not None
        readonly = pinned and not editable_shared
        return {"i": i, "tag": node.tag, "id": node.attrs.get("id", ""), "pinned": pinned, "role": role,
                "label": {"header": "Menu and top of page", "footer": "Footer"}.get(role) or _label(src, node),
                "managed": any("data-shop-sync" in n.attrs for n in node.walk()),
                "readonly": readonly, "up": up, "down": down,
                "fields": [] if readonly else block_fields(doc, node)}

    def role_of(node):
        if parent is body and node is header:
            return "header"
        if parent is body and node is footer:
            return "footer"
        return None

    blocks = []
    if main is not None and header is not None:
        blocks.append(record(header, None, "header", False, False))
    for i, node in enumerate(seq):
        role = role_of(node)
        up = i > 0 and role is None and role_of(seq[i - 1]) is None
        down = i < len(seq) - 1 and role is None and role_of(seq[i + 1]) is None
        blocks.append(record(node, i, role, up, down))
    if main is not None and footer is not None:
        blocks.append(record(footer, None, "footer", False, False))

    title, desc = _title_node(doc), _description_node(doc)
    page_fields = []
    if title is not None and title.close_start is not None:
        page_fields.append({"id": "page:title", "kind": "text", "label": "Page title (browser tab)",
                            "value": display_text(src[title.open_end:title.close_start]), "remove": None,
                            "multiline": False})
    page_fields.append({"id": "page:description", "kind": "text", "label": "Page description (for search engines)",
                        "value": desc.attrs.get("content", "") if desc is not None else "", "remove": None,
                        "multiline": True})
    return {"file": file, "hash": _hash(src), "blocks": blocks, "page_fields": page_fields,
            "shared_editable": editable_shared}


# --------------------------------------------------------------------------- #
# Applying the boxes
# --------------------------------------------------------------------------- #
def _left_trim(src: str, pos: int) -> int:
    while pos > 0 and src[pos - 1] in " \t":
        pos -= 1
    if pos > 0 and src[pos - 1] == "\n":
        pos -= 1
    return pos


def apply_edits(src: str, doc: Doc, payload) -> str:
    errors, edits, attrs, removes = {}, [], {}, []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        fid, value = str(item.get("id", "")), item.get("value")
        try:
            if fid in ("page:title", "page:description"):
                text = _norm_text("" if value is None else str(value))
                if len(text) > 500:
                    raise ValueError("Too long.")
                if fid == "page:title":
                    node = _title_node(doc)
                    if node is None or node.close_start is None:
                        continue
                    if not text:
                        raise ValueError("The page needs a title.")
                    if text != _norm_text(display_text(src[node.open_end:node.close_start])):
                        edits.append((node.open_end, node.close_start, html.escape(text.replace("\n", " "), quote=False)))
                else:
                    node = _description_node(doc)
                    if node is not None:
                        if text != _norm_text(node.attrs.get("content", "")):
                            attrs.setdefault(node.start, {})["content"] = text.replace("\n", " ")
                    elif text:
                        head = doc.find("head")
                        if head is not None and head.close_start is not None:
                            edits.append((head.close_start, head.close_start,
                                          f'  <meta name="description" content="{html.escape(text.replace(chr(10), " "), quote=True)}">\n'))
                continue
            m = _ID_RE.fullmatch(fid)
            node = doc.by_start.get(int(m.group(1))) if m else None
            if node is None:
                raise ValueError("This page changed. Reload it and try again.")
            kind = m.group(2)
            if kind == "remove":
                if value is True:
                    removes.append(node)
                continue
            text = "" if value is None else str(value)
            if len(text) > MAX_TEXT:
                raise ValueError("That is too long.")
            if kind in ("text", "html"):
                if node.close_start is None:
                    raise ValueError("This text cannot be edited here.")
                raw = src[node.open_end:node.close_start]
                if kind == "text":
                    new = _norm_text(text)
                    if new == _norm_text(display_text(raw)):
                        continue
                    if not new:
                        raise ValueError("Type some text here, or press Remove.")
                    edits.append((node.open_end, node.close_start, _encode_text(new)))
                else:
                    new = " ".join(text.split())
                    if new == " ".join(re.sub(r"<!--.*?-->", "", raw, flags=re.S).split()):
                        continue
                    if not new:
                        raise ValueError("Type some text here, or press Remove.")
                    _check_html(new)
                    edits.append((node.open_end, node.close_start, new))
            else:
                name = {"href": "href", "src": "src", "alt": "alt", "ph": "placeholder"}[kind]
                new = text.strip() if kind in ("href", "src") else text.strip()
                if new == node.attrs.get(name, ""):
                    continue
                if kind == "href":
                    _check_href(new)
                elif kind == "src":
                    _check_src(new)
                elif len(new) > 300:
                    raise ValueError("That is too long.")
                attrs.setdefault(node.start, {})[name] = new
        except ValueError as exc:
            errors[fid] = str(exc)
    if errors:
        raise EditError("Check the boxes marked in red.", errors)

    for start, changes in attrs.items():
        node = doc.by_start[start]
        tag_text = src[node.start:node.open_end]
        for name, val in changes.items():
            tag_text = set_attr(tag_text, name, val)
        edits.append((node.start, node.open_end, tag_text))
    ranges = [(_left_trim(src, n.start), n.close_end) for n in removes if n.close_end is not None]
    edits = [e for e in edits if not any(rs <= e[0] and e[1] <= re_ for rs, re_ in ranges)]
    edits += [(rs, re_, "") for rs, re_ in ranges]

    out, last = src, len(src) + 1
    for start, end, text in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        if end > last:
            continue
        out = out[:start] + text + out[end:]
        last = start
    return out


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
PLACEHOLDER = "assets/site/placeholder.svg"
PLACEHOLDER_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="900" viewBox="0 0 1200 900">'
                   '<rect width="1200" height="900" fill="#e5e7eb"/><text x="600" y="450" font-family="sans-serif" '
                   'font-size="48" fill="#6b7280" text-anchor="middle">Add a picture</text></svg>\n')

TEMPLATES = {
    "text": {"label": "Text (heading and paragraph)", "html": """<section class="ss-section">
  <h2>New section</h2>
  <p>Write something about this section here.</p>
</section>"""},
    "text-image": {"label": "Picture and text side by side", "html": f"""<section class="ss-section">
  <div class="ss-split">
    <img src="{PLACEHOLDER}" alt="Describe this picture">
    <div>
      <h2>New section</h2>
      <p>Write something about this section here.</p>
    </div>
  </div>
</section>"""},
    "cards": {"label": "Three cards", "html": """<section class="ss-section">
  <h2>New section</h2>
  <div class="ss-cards">
    <article class="ss-card">
      <h3>First card</h3>
      <p>A short description.</p>
    </article>
    <article class="ss-card">
      <h3>Second card</h3>
      <p>A short description.</p>
    </article>
    <article class="ss-card">
      <h3>Third card</h3>
      <p>A short description.</p>
    </article>
  </div>
</section>"""},
    "cta": {"label": "Call to action (heading, text, button)", "html": """<section class="ss-section ss-cta">
  <h2>New section</h2>
  <p>Tell visitors what to do next.</p>
  <a class="ss-button" href="index.html">Button text</a>
</section>"""},
}

SECTION_CSS = f"""
{CSS_MARKER}
.ss-section{{max-width:72rem;margin:0 auto;padding:3rem 5%}}
.ss-split{{display:grid;gap:2rem;grid-template-columns:repeat(auto-fit,minmax(18rem,1fr));align-items:center}}
.ss-split img{{width:100%;aspect-ratio:4/3;object-fit:cover;border-radius:var(--radius-md,.5rem)}}
.ss-cards{{display:grid;gap:1.5rem;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr))}}
.ss-card{{background:var(--color-white,#fff);color:var(--color-text,inherit);border-radius:var(--radius-md,.5rem);box-shadow:var(--shadow-sm,0 1px 3px rgba(0,0,0,.1));padding:1.25rem}}
.ss-card h3{{margin-top:0}}
.ss-cta{{text-align:center}}
.ss-button{{display:inline-block;padding:.7rem 1.4rem;border-radius:var(--radius-pill,999px);background:var(--color-button-bg,#222);color:var(--color-button-text,#fff);text-decoration:none;font-weight:600}}
.ss-button:hover,.ss-button:focus{{background:var(--color-button-hover-bg,#000);color:var(--color-button-hover-text,#fff)}}
"""


def _ensure_assets(folder: Path) -> None:
    css = folder / "styles.css"
    if css.is_file():
        text = css.read_text(encoding="utf-8")
        if CSS_MARKER not in text:
            _write(css, text.rstrip() + "\n" + SECTION_CSS)
    target = folder / PLACEHOLDER
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(PLACEHOLDER_SVG, encoding="utf-8")


def _index_of(op, seq, key="i"):
    try:
        i = int(op.get(key))
    except (TypeError, ValueError):
        raise EditError("That section was not found.")
    if not 0 <= i < len(seq):
        raise EditError("That section was not found. Reload the page.")
    return i


def structure_op(src: str, op: dict) -> str:
    doc = parse(src)
    body, main, header, footer, parent, seq = _structure(doc)

    def pinned(node):
        return parent is body and (node is header or node is footer)

    action = op.get("action")
    if action == "add":
        template = TEMPLATES.get(str(op.get("template")))
        if template is None:
            raise EditError("Choose a kind of section to add.")
        after = op.get("after")
        if after is None:
            if main is not None:
                pos = parent.close_start
            elif footer is not None and footer in seq:
                pos = footer.start
            else:
                pos = body.close_start if body.close_start is not None else len(src)
        elif after == -1:
            pos = parent.open_end
        else:
            pos = seq[_index_of({"i": after}, seq)].close_end
        pre = "" if src[:pos].endswith("\n") or pos == 0 else "\n"
        return src[:pos] + pre + template["html"] + "\n" + src[pos:]
    i = _index_of(op, seq)
    node = seq[i]
    if pinned(node):
        raise EditError("The menu and footer are shared by every page; they cannot be moved or deleted.")
    if action == "delete":
        return src[:_left_trim(src, node.start)] + src[node.close_end:]
    if action == "duplicate":
        copy = src[node.start:node.close_end]
        if "data-shop-sync" in copy:
            raise EditError("This section is managed by the shop and cannot be duplicated.")
        copy = _ATTR_ID.sub("", copy)
        return src[:node.close_end] + "\n" + copy + src[node.close_end:]
    if action == "move":
        direction = -1 if int(op.get("dir", 1)) < 0 else 1
        j = i + direction
        if not 0 <= j < len(seq) or pinned(seq[j]):
            raise EditError("This section cannot move any further.")
        a, b = (seq[i], seq[j]) if direction > 0 else (seq[j], seq[i])
        return (src[:a.start] + src[b.start:b.close_end] + src[a.close_end:b.start]
                + src[a.start:a.close_end] + src[b.close_end:])
    raise EditError("Unknown action.")


# --------------------------------------------------------------------------- #
# Files, history, shared menu and footer
# --------------------------------------------------------------------------- #
def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    os.replace(tmp, path)


def _backup(folder: Path, name: str, text: str) -> None:
    hist = folder / HISTORY_DIR
    hist.mkdir(exist_ok=True)
    (hist / f"{name}.{time.time_ns():020d}.bak").write_text(text, encoding="utf-8", newline="\n")
    for old in sorted(hist.glob(f"{name}.*.bak"))[:-HISTORY_KEEP]:
        old.unlink(missing_ok=True)


def _ids(page: str) -> set:
    return set(re.findall(r"""\sid\s*=\s*["']([^"']+)["']""", page))


def _for_subpage(fragment: str, index_ids: set) -> str:
    """Make a copy of the homepage header/footer work from another page (same as shop_cart.py)."""
    def fix(match):
        name = match.group(2)
        return f'{match.group(1)}"index.html#{name}"' if name in index_ids else match.group(0)
    fragment = re.sub(r"""(<a\b[^>]*?\shref\s*=\s*)["']#([A-Za-z][^"']*)["']""", fix, fragment, flags=re.I)
    first = re.search(r"""<a\b[^>]*?\shref\s*=\s*(["'])(.*?)\1""", fragment, re.S | re.I)
    if first and first.group(2).strip() in ("", "#") and re.match(r"\s*<header", fragment, re.I):
        fragment = fragment[:first.start(2)] + "index.html" + fragment[first.end(2):]
    return fragment


def _shared(src: str):
    doc = parse(src)
    _, _, header, footer, _, _ = _structure(doc)
    return (src[header.start:header.close_end] if header else "",
            src[footer.start:footer.close_end] if footer else "")


def _apply_ranges(text: str, reps) -> str:
    out, last = text, len(text) + 1
    for start, end, new in sorted(reps, key=lambda r: (r[0], r[1]), reverse=True):
        if end > last:
            continue
        out = out[:start] + new + out[end:]
        last = start
    return out


def sync_shared(folder) -> list:
    """Copy the Home page's menu and footer to every other page. Returns the pages changed."""
    folder = Path(folder)
    src = (folder / "index.html").read_text(encoding="utf-8")
    doc = parse(src)
    _, _, header, footer, _, _ = _structure(doc)
    ids = _ids(src)
    new_header = _for_subpage(src[header.start:header.close_end], ids) if header else None
    new_footer = _for_subpage(src[footer.start:footer.close_end], ids) if footer else None
    changed = []
    for path in sorted(folder.glob("*.html")):
        if path.name == "index.html" or path.name.startswith("."):
            continue
        text = path.read_text(encoding="utf-8")
        d = parse(text)
        body, _, h, f, _, _ = _structure(d)
        if body.tag != "body":
            continue
        reps = []
        if new_header:
            reps.append((h.start, h.close_end, new_header) if h else (body.open_end, body.open_end, "\n" + new_header))
        if new_footer:
            reps.append((f.start, f.close_end, new_footer) if f else
                        (body.close_start, body.close_start, new_footer + "\n"))
        new = _apply_ranges(text, reps)
        if new != text:
            _write(path, new)
            changed.append(path.name)
    return changed


def _clone_item(src: str, li: Node, href: str, text: str) -> str:
    raw = _ATTR_ID.sub("", src[li.start:li.close_end])
    a = next((n for n in parse(raw).root.walk() if n.tag == "a"), None)
    if a is None or a.close_start is None:
        return f'<li><a href="{html.escape(href, quote=True)}">{html.escape(text, quote=False)}</a></li>'
    return (raw[:a.start] + set_attr(raw[a.start:a.open_end], "href", href)
            + html.escape(text, quote=False) + raw[a.close_start:])


def _first_ul(node):
    return next((n for n in node.walk() if n.tag == "ul"), None)


def _add_menu_item(folder: Path, file: str, title: str) -> bool:
    path = folder / "index.html"
    src = path.read_text(encoding="utf-8")
    doc = parse(src)
    _, _, header, footer, _, _ = _structure(doc)
    reps, present = [], False
    for part in (header, footer):
        ul = _first_ul(part) if part is not None else None
        if ul is None:
            continue
        items = [c for c in ul.children if c.tag == "li" and "data-shop-link" not in c.attrs]
        if not items:
            continue
        if any(n.tag == "a" and n.attrs.get("href") == file for n in ul.walk()):
            present = True
            continue
        last = items[-1]
        line_start = src.rfind("\n", 0, last.start) + 1
        indent = src[line_start:last.start] if not src[line_start:last.start].strip() else ""
        reps.append((last.close_end, last.close_end, "\n" + indent + _clone_item(src, last, file, title)))
    if not reps:
        return present
    _backup(folder, "index.html", src)
    _write(path, _apply_ranges(src, reps))
    return True


def _remove_menu_items(folder: Path, file: str) -> None:
    path = folder / "index.html"
    src = path.read_text(encoding="utf-8")
    doc = parse(src)
    _, _, header, footer, _, _ = _structure(doc)
    reps = []
    for part in (header, footer):
        if part is None:
            continue
        for li in part.walk():
            if li.tag != "li" or li.close_end is None:
                continue
            first = next((n for n in li.walk() if n.tag == "a"), None)
            if first is not None and first.attrs.get("href") == file:
                reps.append((_left_trim(src, li.start), li.close_end, ""))
    if reps:
        _backup(folder, "index.html", src)
        _write(path, _apply_ranges(src, reps))


# --------------------------------------------------------------------------- #
# What the Studio calls
# --------------------------------------------------------------------------- #
def is_generated(folder: Path, file: str) -> bool:
    if file in GENERATED:
        return True
    try:
        text = (folder / file).read_text(encoding="utf-8")
    except OSError:
        return False
    return any(mark in text for mark in GENERATED_MARKS)


def _page_path(folder: Path, file: str) -> Path:
    if not FILE_RE.match(str(file)):
        raise ValueError("Unknown page.")
    path = folder / file
    if not path.is_file():
        raise ValueError("Unknown page.")
    return path


def _title_of(text: str, fallback: str) -> str:
    m = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
    return (display_text(m.group(1)) if m else "") or fallback


def list_pages(folder) -> list:
    folder = Path(folder)
    files = sorted((p for p in folder.glob("*.html") if FILE_RE.match(p.name)),
                   key=lambda p: (p.name != "index.html", p.name))
    out = []
    for p in files:
        gen = is_generated(folder, p.name)
        out.append({"file": p.name, "title": _title_of(p.read_text(encoding="utf-8", errors="replace"), p.name),
                    "editable": not gen, "deletable": not gen and p.name not in PROTECTED})
    return out


def outline_for(folder, file: str) -> dict:
    folder = Path(folder).resolve()
    path = _page_path(folder, file)
    src = path.read_text(encoding="utf-8")
    generated = is_generated(folder, file)
    out = build_outline(src, file, editable_shared=(file == "index.html"))
    if generated:
        out["blocks"], out["page_fields"] = [], []
    index = (folder / "index.html").read_text(encoding="utf-8")
    ids = [n.attrs["id"] for n in _structure(parse(index))[5] if n.attrs.get("id")]
    pages = list_pages(folder)
    out.update({"pages": pages, "generated": generated,
                "templates": [{"key": k, "label": v["label"]} for k, v in TEMPLATES.items()],
                "targets": [p["file"] for p in pages] + [f"index.html#{i}" for i in ids],
                "can_undo": any((folder / HISTORY_DIR).glob(f"{file}.*.bak")) if (folder / HISTORY_DIR).is_dir() else False,
                "message": ""})
    return out


def edit(folder, file: str, expected_hash: str, fields, op) -> dict:
    folder = Path(folder).resolve()
    path = _page_path(folder, file)
    if is_generated(folder, file):
        raise ValueError("This page is made by the shop. Use Edit shop items to change it.")
    src = path.read_text(encoding="utf-8")
    if expected_hash and _hash(src) != expected_hash:
        raise Conflict("This page was changed somewhere else, so it was reloaded. Please make your change again.")
    new = src
    if fields:
        new = apply_edits(src, parse(src), fields)
    if op:
        if not isinstance(op, dict):
            raise EditError("Unknown action.")
        new = structure_op(new, op)
        if op.get("action") == "add":
            _ensure_assets(folder)
    message = "Nothing was changed."
    if new != src:
        _backup(folder, file, src)
        _write(path, new)
        message = "Saved."
        if file == "index.html" and _shared(src) != _shared(new):
            synced = sync_shared(folder)
            if synced:
                message = f"Saved. The menu and footer were copied to {len(synced)} other page(s)."
    out = outline_for(folder, file)
    out["message"] = message
    return out


def add_page(folder, title: str, in_menu: bool = True) -> dict:
    folder = Path(folder).resolve()
    title = " ".join(str(title).split())
    if not title or len(title) > 60:
        raise EditError("Type a page name (up to 60 characters).", {"title": "Type a page name."})
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-") or "page"
    if slug in {p.removesuffix(".html") for p in PROTECTED} or slug.startswith(("shop-", "blog-", "newsletter")) or slug == "blog":
        slug += "-page"
    base, n = slug, 2
    while (folder / f"{slug}.html").exists():
        slug, n = f"{base}-{n}", n + 1
    file = f"{slug}.html"

    index = (folder / "index.html").read_text(encoding="utf-8")
    doc = parse(index)
    body, _, header, footer, _, _ = _structure(doc)
    head = doc.find("head")
    head_inner = index[head.open_end:head.close_start] if head is not None and head.close_start is not None else \
        '  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1">\n' \
        '  <link rel="stylesheet" href="styles.css">\n'
    site = _title_of(index, folder.name)
    site = site.split("|")[-1].strip() or site
    head_inner = re.sub(r"<title>.*?</title>", lambda m: f"<title>{html.escape(title + ' | ' + site, quote=False)}</title>",
                        head_inner, count=1, flags=re.S | re.I)
    head_inner = re.sub(r'\s*<meta\b[^>]*name\s*=\s*["\']description["\'][^>]*>', "", head_inner, flags=re.I)
    ids = _ids(index)
    hdr = _for_subpage(index[header.start:header.close_end], ids) if header else ""
    ftr = _for_subpage(index[footer.start:footer.close_end], ids) if footer else ""
    scripts = "\n".join(index[n.start:n.close_end] for n in body.children if n.tag == "script" and n.attrs.get("src"))
    lang = (doc.find("html").attrs.get("lang") if doc.find("html") is not None else "") or "en"
    page = f"""<!doctype html>
<html lang="{html.escape(lang, quote=True)}">
<head>
{head_inner.strip(chr(10))}
</head>
<body>
{hdr}

<main id="main" class="ss-page">
  <section class="ss-section">
    <h1>{html.escape(title, quote=False)}</h1>
    <p>Write something about this page here.</p>
  </section>
</main>

{ftr}
{scripts}
</body>
</html>
"""
    _write(folder / file, page)
    _ensure_assets(folder)
    menu = False
    if in_menu:
        menu = _add_menu_item(folder, file, title)
        if menu:
            sync_shared(folder)
    out = outline_for(folder, file)
    out["message"] = f"Created {file}." + (" It was added to the menu on every page." if menu else
                                           " It is not in the menu." if not in_menu else
                                           " It could not be added to the menu (no menu list was found).")
    return out


def delete_page(folder, file: str) -> dict:
    folder = Path(folder).resolve()
    path = _page_path(folder, file)
    if file in PROTECTED or is_generated(folder, file):
        raise ValueError("The Home, Shop and Cart pages cannot be deleted here.")
    _backup(folder, f"deleted-{file}", path.read_text(encoding="utf-8"))
    path.unlink()
    _remove_menu_items(folder, file)
    sync_shared(folder)
    leftover = 0
    for other in folder.glob("*.html"):
        leftover += len(re.findall(r"""href\s*=\s*["']%s(?:#[^"']*)?["']""" % re.escape(file),
                                   other.read_text(encoding="utf-8", errors="replace")))
    out = outline_for(folder, "index.html")
    out["message"] = f"Deleted {file} and its menu links." + (
        f" {leftover} other link(s) on the site still point to it; edit them to fix." if leftover else "")
    return out


def undo(folder, file: str) -> dict:
    folder = Path(folder).resolve()
    path = _page_path(folder, file)
    backups = sorted((folder / HISTORY_DIR).glob(f"{file}.*.bak")) if (folder / HISTORY_DIR).is_dir() else []
    if not backups:
        raise ValueError("There is nothing to undo for this page.")
    before = path.read_text(encoding="utf-8")
    text = backups[-1].read_text(encoding="utf-8")
    _write(path, text)
    backups[-1].unlink()
    if file == "index.html" and _shared(before) != _shared(text):
        sync_shared(folder)
    out = outline_for(folder, file)
    out["message"] = "Went back to the previous version of this page."
    return out


# --------------------------------------------------------------------------- #
# Helpers for generated pages (blog, newsletter, ...)
# --------------------------------------------------------------------------- #
GENERATED_MARKS = ("<!-- studio-shop-page -->", "<!-- studio-news-page -->", "<!-- studio-blog-page -->")


def page_shell(folder, title: str, noindex: bool = False, extra_head: str = ""):
    """(head html, header html, footer html) copied from the Home page so a generated page matches the site."""
    folder = Path(folder)
    index = (folder / "index.html").read_text(encoding="utf-8")
    doc = parse(index)
    body, _m, header, footer, _p, _s = _structure(doc)
    head = doc.find("head")
    head_inner = index[head.open_end:head.close_start] if head is not None and head.close_start is not None else \
        '  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1">\n  <link rel="stylesheet" href="styles.css">\n'
    site = _title_of(index, folder.name)
    site = site.split("|")[-1].strip() or site
    full = f"{title} | {site}" if title else site
    head_inner = re.sub(r"<title>.*?</title>", lambda m: f"<title>{html.escape(full, quote=False)}</title>",
                        head_inner, count=1, flags=re.S | re.I)
    head_inner = re.sub(r'\s*<meta\b[^>]*name\s*=\s*["\']description["\'][^>]*>', "", head_inner, flags=re.I)
    if noindex:
        head_inner += '\n  <meta name="robots" content="noindex">'
    if extra_head:
        head_inner += "\n  " + extra_head
    ids = _ids(index)
    hdr = _for_subpage(index[header.start:header.close_end], ids) if header else ""
    ftr = _for_subpage(index[footer.start:footer.close_end], ids) if footer else ""
    return head_inner.strip("\n"), hdr, ftr
