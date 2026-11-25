"""Page expander: turns a generated homepage's placeholder links into real pages.

1. Finds links whose href is "#". Navigation, header and footer links are planned by plain
   code (no model needed): Home goes to the main page and every other one gets its own page.
   Any remaining links in the page body are planned by the model (an existing section, the
   homepage, a new page, or nowhere); if that call fails, the rest of the work still happens.
2. Generates the main content of each new page with the model; the homepage's own
   header and footer are reused so every page is consistent.
3. Rewrites the links, adds CSS for classes the new pages need, then writes everything.
4. Makes every page link back to the main page and reports any link that still goes nowhere.

Run on its own:  python -m studio_core.page_expander PROJECT_ID
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from html import escape
from pathlib import Path

from .agent_runtime import _check_invented_specifics, _check_no_js_dependence, _clean_output
from .config import MODELS
from .groq_router import BudgetExceededError, GenerationError, GroqRouter
from .html_assembler import validate_css, validate_fragment
from .storage import Store

MAX_PAGES = int(os.getenv("STUDIO_MAX_PAGES", "8"))
RESERVED = {"index", "styles", "assets", "shop", "cart", "track"}

_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
_DEAD_HREF = re.compile(r"""(?:^|\s)href\s*=\s*(["'])\s*#?\s*\1""", re.I)
_SOCIAL = re.compile(
    r"facebook|twitter|instagram|linkedin|youtube|tiktok|pinterest|snapchat|whatsapp|\bx\b", re.I)


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).replace("&amp;", "&").split())


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Reading the homepage
# --------------------------------------------------------------------------- #
def find_dead_links(html: str) -> list:
    links = []
    for match in _ANCHOR_RE.finditer(html):
        attrs, inner = match.group(1), match.group(2)
        if not _DEAD_HREF.search(attrs):
            continue
        aria = re.search(r"""aria-label\s*=\s*["']([^"']*)["']""", attrs, re.I)
        alt = re.search(r"""\salt\s*=\s*["']([^"']*)["']""", inner, re.I)
        label = _text(inner) or (aria.group(1) if aria else "") or (alt.group(1) if alt else "")
        before = html[max(0, match.start() - 1500):match.start()]
        headings = re.findall(r"<h[1-6][^>]*>(.*?)</h[1-6]>", before, re.S | re.I)
        links.append({
            "id": len(links) + 1, "start": match.start(), "end": match.end(),
            "label": " ".join(label.split())[:80],
            "under": _text(headings[-1])[:60] if headings else "",
        })
    return links


def _section_anchors(html: str) -> dict:
    """id -> heading text (or tag) for every element that has an id."""
    anchors = {}
    for match in re.finditer(r"<(\w+)\b[^>]*?\sid\s*=\s*[\"']([^\"']+)[\"'][^>]*>", html):
        heading = re.search(r"<h[1-6][^>]*>(.*?)</h[1-6]>", html[match.end():match.end() + 800],
                            re.S | re.I)
        anchors[match.group(2)] = _text(heading.group(1))[:60] if heading else match.group(1)
    return anchors


def _classes(html: str) -> list:
    seen = []
    for match in re.finditer(r"""class\s*=\s*["']([^"']*)["']""", html):
        for name in match.group(1).split():
            if name not in seen:
                seen.append(name)
    return seen


def _extract(html: str, tag: str) -> str:
    match = re.search(rf"<{tag}\b.*?</{tag}>", html, re.S | re.I)
    return match.group(0) if match else ""


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def _json_clean(text: str) -> str:
    text = _clean_output(text)
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


def _parse_plan(text: str, link_ids: set, anchors: dict, known: dict = None):
    """`known` maps the slugs of pages that were already planned by code to their titles."""
    known = known or {}
    try:
        plan = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON ({exc.msg}); return only the JSON object")
    if not isinstance(plan, dict):
        raise ValueError("return one JSON object")
    pages, links = plan.get("pages"), plan.get("links")
    if not isinstance(pages, list) or not isinstance(links, dict):
        raise ValueError('JSON needs a "pages" list and a "links" object')
    if len(pages) > MAX_PAGES:
        raise ValueError(f"plan at most {MAX_PAGES} new pages")
    slugs = set(known)
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("each page must be an object")
        slug = str(page.get("slug", "")).strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", slug) or slug in RESERVED:
            raise ValueError(f"invalid page slug {slug!r}; use lowercase letters, digits, hyphens")
        if slug in slugs:
            raise ValueError(f"duplicate page slug {slug!r}")
        title, brief = str(page.get("title", "")).strip(), str(page.get("brief", "")).strip()
        if not title or not brief:
            raise ValueError(f"page {slug!r} needs a title and a brief")
        page.update(slug=slug, title=title[:80], brief=brief[:300])
        slugs.add(slug)
    targets = {}
    for key, value in links.items():
        try:
            link_id = int(key)
        except (TypeError, ValueError):
            raise ValueError(f"link key {key!r} is not a number")
        if link_id not in link_ids:
            continue  # ids we did not ask about are ignored
        value = str(value).strip()
        if value in ("skip", "index") or value in slugs:
            targets[link_id] = value
        elif value.startswith("#") and value[1:] in anchors:
            targets[link_id] = value
        elif value in anchors:
            targets[link_id] = "#" + value
        else:
            raise ValueError(f"link {key} -> {value!r} is unknown; use a page slug, an "
                             "existing #section id, 'index' or 'skip'")
    used = set(targets.values())
    return [p for p in pages if p["slug"] in used], targets


# --------------------------------------------------------------------------- #
# Planning navigation links without a model
# --------------------------------------------------------------------------- #
HOME_WORDS = {"home", "homepage", "home page", "main page", "main", "back to home",
              "back to the main page", "back to main page", "start", "index"}
SHOP_WORDS = {"shop": "shop", "store": "shop", "our shop": "shop", "online store": "shop",
              "cart": "cart", "my cart": "cart", "your cart": "cart", "basket": "cart",
              "checkout": "cart"}


def _norm(label: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(label).lower()).split())


def _slug_from_label(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(label).lower()).strip("-")[:40].strip("-") or "page"
    return f"{slug}-page" if slug in RESERVED else slug


def _chrome_spans(html: str) -> list:
    """Character ranges of <nav>, <header> and <footer>: links there are site navigation."""
    spans = []
    for tag in ("nav", "header", "footer"):
        spans += [(m.start(), m.end()) for m in re.finditer(rf"<{tag}\b.*?</{tag}>", html, re.S | re.I)]
    return spans


def _in_chrome(position: int, spans: list) -> bool:
    return any(start <= position < end for start, end in spans)


def _section_snippet(html: str, section_id: str) -> str:
    match = re.search(rf"""\sid\s*=\s*["']{re.escape(section_id)}["']""", html)
    return _text(html[match.end():match.end() + 1800])[:240] if match else ""


def _plan_navigation(html, candidates, anchors, site, purpose, spans, shop_on):
    """Decide the target of every navigation link with plain code.
    Returns (pages, targets, leftover_candidates)."""
    pages, targets, leftover, by_key = [], {}, [], {}
    site_key = _norm(site)
    for link in candidates:
        key = _norm(link["label"])
        if key in HOME_WORDS or (site_key and key == site_key):
            targets[link["id"]] = "index"
        elif shop_on and key in SHOP_WORDS:
            targets[link["id"]] = SHOP_WORDS[key]
        elif not _in_chrome(link["start"], spans):
            leftover.append(link)
        elif key in by_key:
            targets[link["id"]] = by_key[key]
        elif len(pages) >= MAX_PAGES:
            leftover.append(link)
        else:
            slug, n = _slug_from_label(link["label"]), 2
            taken = {p["slug"] for p in pages}
            while slug in taken:
                slug = f"{_slug_from_label(link['label'])}-{n}"
                n += 1
            match = next((aid for aid, heading in anchors.items()
                          if _norm(heading) == key or (key and key in _norm(heading))), None)
            snippet = _section_snippet(html, match) if match else ""
            brief = (f"A full page for the '{link['label']}' link of the {site} website. "
                     + (f"The homepage has a short section on this: {snippet} Expand it into a complete page. "
                        if snippet else "") + (f"Site purpose: {purpose[:120]}" if purpose else ""))
            pages.append({"slug": slug, "title": link["label"][:80], "brief": brief[:300],
                          "description": f"{link['label']} - {site}"[:160]})
            by_key[key] = slug
            targets[link["id"]] = slug
    return pages, targets, leftover


def verify_links(root: Path) -> dict:
    """Plain-code check of every page: links that still go nowhere, and links to missing files."""
    dead, broken = [], []
    pages = {p.name: p.read_text(encoding="utf-8") for p in sorted(root.glob("*.html"))}
    ids = {name: _ids_of(text) for name, text in pages.items()}
    for name, text in pages.items():
        for match in _ANCHOR_RE.finditer(text):
            attrs, inner = match.group(1), match.group(2)
            label = _text(inner)[:40]
            href = re.search(r"""href\s*=\s*(["'])(.*?)\1""", attrs, re.I)
            if not href:
                continue
            value = href.group(2).strip()
            if value in ("", "#"):
                if label and not _SOCIAL.search(label) and not label.startswith("["):
                    dead.append(f"{name}: '{label}'")
            elif value.startswith(("http://", "https://", "mailto:", "tel:", "javascript:")):
                continue
            elif value.startswith("#"):
                if value[1:] not in ids[name]:
                    broken.append(f"{name}: {value}")
            else:
                target, _, frag = value.partition("#")
                if target not in pages and not (root / target.split("?")[0]).is_file():
                    broken.append(f"{name}: {value} (file missing)")
                elif frag and target in ids and frag not in ids[target]:
                    broken.append(f"{name}: {value} (section missing)")
    return {"dead": dead, "broken": broken, "pages": len(pages)}


def _ids_of(page: str) -> set:
    return set(re.findall(r"""\sid\s*=\s*["']([^"']+)["']""", page))


# --------------------------------------------------------------------------- #
# Rewriting links
# --------------------------------------------------------------------------- #
def _href(target: str) -> str:
    if target == "index":
        return "index.html"
    return target if target.startswith("#") else f"{target}.html"     # also "shop" -> shop.html


def apply_plan(html: str, links: list, targets: dict, subpage: bool) -> str:
    out, pos = [], 0
    for link in links:
        out.append(html[pos:link["start"]])
        tag = html[link["start"]:link["end"]]
        target = targets.get(link["id"])
        if target and target != "skip":
            tag = _DEAD_HREF.sub(lambda m, h=_href(target): f' href="{h}"', tag, count=1)
        out.append(tag)
        pos = link["end"]
    out.append(html[pos:])
    text = "".join(out)
    if subpage:  # section links must point back to the homepage
        text = re.sub(r'''(<a\b[^>]*?\shref=")#([A-Za-z][^"]*")''', r"\1index.html#\2", text)
    return text


def _sanitize_page(fragment: str, root: Path, allowed: set) -> str:
    """Remove harmless junk from a page body (never adds content)."""
    text = re.sub(r"<!--.*?-->", "", fragment, flags=re.S)
    first, last = text.find("<"), text.rfind(">")
    if first != -1 and last > first:   # drop chatter before/after the markup
        text = text[first:last + 1]
    text = re.sub(r"""\sstyle\s*=\s*("[^"]*"|'[^']*')""", "", text, flags=re.I)

    def fix_img(match):
        tag = match.group(0)
        src = re.search(r"""\ssrc\s*=\s*["']([^"']*)["']""", tag, re.I)
        value = src.group(1).strip() if src else ""
        ok = value.startswith(("http://", "https://", "data:")) or (value and (root / value).is_file())
        return tag if ok else ""
    text = re.sub(r"<img\b[^>]*>", fix_img, text, flags=re.I)

    def fix_href(match):
        value = match.group(2).strip()
        if value.startswith(("http://", "https://", "mailto:", "tel:")) or value == "#" or value in allowed:
            return match.group(0)
        return f'{match.group(1)}"#"'
    text = re.sub(r"""(<a\b[^>]*?\shref\s*=\s*)["']([^"']*)["']""", fix_href, text, flags=re.I).strip()
    return _normalize_headings(text)


def _normalize_headings(text: str) -> str:
    """A page needs exactly one <h1>. Fix the two common slips in code instead of spending a
    model repair: extra <h1> become <h2>; no <h1> promotes the first <h2>."""
    count = len(re.findall(r"<h1\b", text, re.I))
    if count > 1:
        first = re.search(r"<h1\b.*?</h1>", text, re.S | re.I)
        if first:
            rest = re.sub(r"<(/?)h1\b", r"<\1h2", text[first.end():], flags=re.I)
            return text[:first.end()] + rest
    if count == 0:
        return re.sub(r"<h2\b([^>]*)>(.*?)</h2>", r"<h1\1>\2</h1>", text, count=1, flags=re.S | re.I)
    return text


def _validate_main(text: str, title: str, known_text: str) -> None:
    validate_fragment(text, title)
    if len(re.findall(r"<h1\b", text, re.I)) != 1:
        raise ValueError("the page must contain exactly one <h1>")
    for tag in ("header", "footer"):
        if re.search(rf"<{tag}\b", text, re.I):
            raise ValueError(f"do not include <{tag}>; the site header and footer are added automatically")
    _check_invented_specifics(text, known_text)
    _check_no_js_dependence(text)


def _document(site: str, title: str, description: str, header: str, main: str, footer: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="{escape(description, quote=True)}">
  <title>{escape(title)} | {escape(site)}</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
{header}

{main}

{footer}
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# The agent
# --------------------------------------------------------------------------- #
def _load_settings(root: Path) -> dict:
    try:
        data = json.loads((root / "site_settings.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class PageExpander:
    def __init__(self, project, store=None, router=None):
        self.project = project
        self.store = store or Store()
        self.router = router or GroqRouter(self.store)
        self.root = Path(project["root_path"]).resolve()

    def _models(self):
        return [MODELS["dev_fallback"], MODELS["pm"], MODELS["dev_agent"]]

    def _finish_links(self) -> str:
        """Make every page link back to the main page, then report what still goes nowhere."""
        try:
            from . import site_extras
            print(f"[pages] {site_extras.ensure_home_links(self.root)}", flush=True)
        except ImportError:
            pass
        report = verify_links(self.root)
        text = f"{report['pages']} page(s) checked"
        if report["dead"]:
            shown = "; ".join(report["dead"][:6]) + (" ..." if len(report["dead"]) > 6 else "")
            text += f"; {len(report['dead'])} link(s) still go nowhere ({shown})"
        if report["broken"]:
            text += f"; {len(report['broken'])} broken link(s) ({'; '.join(report['broken'][:6])})"
        if not report["dead"] and not report["broken"]:
            text += "; every link opens a page or section"
        print(f"[pages] Link check: {text}", flush=True)
        return text

    def run(self) -> str:
        if hasattr(self.router, "reset_budget"):
            self.router.reset_budget()
        index_path = self.root / "index.html"
        if not index_path.is_file():
            raise RuntimeError("This project has no index.html to expand.")
        html = index_path.read_text(encoding="utf-8")

        all_links = find_dead_links(html)
        candidates = [l for l in all_links if l["label"] and not l["label"].startswith("[")
                      and not _SOCIAL.search(l["label"])][:40]
        if not candidates:
            return "No placeholder links found; nothing to do. " + self._finish_links()
        anchors = _section_anchors(html)
        site = self.project["name"]
        purpose = str(self.project.get("brief") or "")
        shop_on = bool(_load_settings(self.root).get("shop", {}).get("enabled"))
        print(f"[pages] {len(candidates)} placeholder links found; planning the site map ...", flush=True)

        # ---- 1. navigation, header and footer links: planned by code ------------- #
        pages, targets, leftover = _plan_navigation(
            html, candidates, anchors, site, purpose, _chrome_spans(html), shop_on)
        if pages:
            print("[pages] Navigation pages: " + ", ".join(f"{p['slug']}.html" for p in pages), flush=True)

        # ---- 2. links inside the page body: planned by the model ----------------- #
        if leftover:
            link_lines = "\n".join(f'{l["id"]}|{l["label"]}|under: {l["under"] or "-"}' for l in leftover)
            section_lines = "\n".join(f"#{k} ({v})" for k, v in list(anchors.items())[:30])
            known = {p["slug"]: p["title"] for p in pages}
            known_lines = "\n".join(f"{k}|{v}" for k, v in known.items()) or "none"
            room = max(0, MAX_PAGES - len(pages))
            messages = [
                {"role": "system", "content": (
                    "You are a website architect. Decide where each placeholder link of a "
                    "website should go. Return ONLY one JSON object, no commentary.")},
                {"role": "user", "content": (
                    f"Site: {site}\nSite purpose: {purpose[:400]}\n\n"
                    f"Sections that already exist on the homepage:\n{section_lines}\n\n"
                    f"Pages that will exist (slug|title):\n{known_lines}\n\n"
                    f"Placeholder links (id|label|under heading):\n{link_lines}\n\n"
                    'Return {"pages":[{"slug":"kebab-case","title":"...","brief":"what the page '
                    'covers, one or two sentences"}],"links":{"<id>":"<target>"}}.\n'
                    "Targets: an existing section as \"#sectionid\"; \"index\" for Home or the logo; "
                    "a slug from \"Pages that will exist\" or from \"pages\"; or \"skip\" (anything "
                    "that is not navigation). Prefer sending a link to a page that will exist, or to "
                    "the section it names, over creating a new page. Links on the same topic share "
                    f"one page. Create at most {room} new pages; give every new page at least one link.")},
            ]
            ids = {l["id"] for l in leftover}
            try:
                text, _ = self.router.generate(
                    [MODELS["pm"], MODELS["dev_fallback"]], messages,
                    lambda t: _parse_plan(t, ids, anchors, known),
                    estimated_out_tokens=1800, label="Page plan", clean=_json_clean)
                extra_pages, extra_targets = _parse_plan(text, ids, anchors, known)
                extra_pages = extra_pages[:room]
                keep = {p["slug"] for p in extra_pages} | set(known)
                targets.update({k: v for k, v in extra_targets.items()
                                if v in ("index", "skip") or v.startswith("#") or v in keep})
                pages += extra_pages
            except (GenerationError, BudgetExceededError, ValueError) as exc:
                print(f"[pages] Could not plan the body links ({exc}); the navigation pages "
                      "will still be created.", flush=True)

        if not pages:
            changed = self._write_index_only(html, all_links, targets)
            return ("The links now point at the main page and existing sections. " if changed else
                    "The plan found no new pages to create. ") + self._finish_links()
        print("[pages] Plan: " + ", ".join(f"{p['slug']}.html" for p in pages), flush=True)

        # ---- 3. write each page's main content ---------------------------------- #
        hero = re.search(r"<h1\b[^>]*>(.*?)</h1>", html, re.S | re.I)
        homepage_text = _text(html)
        known_text = f"{self.project.get('brief') or ''} {homepage_text}"
        css_path = self.root / "styles.css"
        css = css_path.read_text(encoding="utf-8") if css_path.is_file() else ""
        classes = ", ".join(_classes(html))[:900]
        assets = sorted(str(f.relative_to(self.root)) for f in (self.root / "assets").rglob("*")
                        if f.is_file() and f.name != "CREDITS.txt") if (self.root / "assets").is_dir() else []
        allowed = {"index.html"} | {f"index.html#{a}" for a in anchors} | {f"{p['slug']}.html" for p in pages}
        if shop_on:
            allowed |= {"shop.html", "cart.html"}
        link_list = ", ".join(sorted(allowed))[:900]
        labels = {}
        for lid, target in targets.items():
            labels.setdefault(target, []).append(next(l["label"] for l in candidates if l["id"] == lid))

        mains, failed, partial = {}, [], False
        for page in pages:
            print(f"[pages] Writing {page['slug']}.html ...", flush=True)
            msgs = [
                {"role": "system", "content": (
                    "You write the MAIN content of one page of a small website. Return ONLY raw "
                    "HTML for a single <main> element: exactly one <h1>, then 2 to 4 <section> "
                    "blocks of real, useful, readable text. No header, footer, Markdown, comments, "
                    "inline styles, or SVG. Reuse these existing CSS class names where they fit: "
                    f"{classes}. Do not invent prices, years, times, phone numbers, statistics or "
                    "product specifications. If the topic carries safety risks (electricity, "
                    "chemicals, fuel, heights), give general educational information only, no "
                    "specific ratings or step-by-step installation instructions, and say that a "
                    "qualified professional should be consulted. Link only to: "
                    f"{link_list}. Images only from these local files, or none: "
                    f"{', '.join(assets) or 'none'}.")},
                {"role": "user", "content": (
                    f"Site: {site}\nHomepage headline: {_text(hero.group(1)) if hero else site}\n"
                    f"Page title: {page['title']}\nPage brief: {page['brief']}\n"
                    f"Linked from the homepage as: {', '.join(labels.get(page['slug'], []))[:200]}")},
            ]
            try:
                main, _ = self.router.generate(
                    self._models(), msgs,
                    lambda t, ti=page["title"]: _validate_main(t, ti, known_text),
                    estimated_out_tokens=1400, label=f"Page {page['slug']}",
                    clean=lambda raw: _sanitize_page(_clean_output(raw), self.root, allowed),
                )
                mains[page["slug"]] = main
            except BudgetExceededError:
                print("[pages] Token budget reached; finishing with the pages completed so far.", flush=True)
                partial = True
                break
            except GenerationError as exc:
                print(f"[pages] Could not write {page['slug']}.html ({exc}); its links stay as '#'.", flush=True)
                failed.append(page["slug"])

        final_targets = {lid: t for lid, t in targets.items()
                         if t in ("index", "skip", "shop", "cart") or t.startswith("#") or t in mains}
        planned = len(pages)
        pages = [p for p in pages if p["slug"] in mains]
        if not pages:
            self._write_index_only(html, all_links, final_targets)
            self._finish_links()
            raise RuntimeError(f"None of the {planned} planned pages could be written "
                               "(see the lines above for why). Home and section links were still fixed; "
                               "run 'Add linked pages' again to retry.")

        # ---- 4. CSS for classes the new pages use that styles.css lacks ---------- #
        defined = set(re.findall(r"\.([A-Za-z_][\w-]*)", css))
        variables = ", ".join(dict.fromkeys(re.findall(r"--[a-zA-Z0-9_-]+", css)))[:500]
        extra_css = ""
        for page in pages:
            main = mains[page["slug"]]
            missing = [c for c in _classes(main) if c not in defined]
            if not missing:
                continue
            print(f"[pages] Styling {page['slug']}.html ({len(missing)} new classes) ...", flush=True)
            try:
                part, _ = self.router.generate(
                    self._models(), [
                        {"role": "system", "content": (
                            "You are an expert CSS designer. Return only complete CSS, no HTML, "
                            "Markdown or explanations. Match the existing site: reuse its variables, "
                            "modern spacing, readable typography, responsive layout. Every spacing "
                            "value needs a unit (for example 1rem).")},
                        {"role": "user", "content": (
                            f"Existing CSS variables (use them): {variables or 'none'}\n"
                            f"Write CSS ONLY for these classes: {', '.join('.' + c for c in missing)}\n"
                            f"HTML that uses them:\n{main[:3000]}")}],
                    validate_css, estimated_out_tokens=1200, label=f"CSS {page['slug']}",
                    clean=_clean_output)
                extra_css += f"\n/* PAGE: {page['title']} */\n{part}\n"
                defined.update(missing)
            except BudgetExceededError:
                partial = True
                break
            except GenerationError as exc:
                print(f"[pages] Styling for {page['slug']} failed ({exc}); page will use base styles.", flush=True)

        # ---- 5. write everything ------------------------------------------------ #
        sub_html = apply_plan(html, all_links, final_targets, subpage=True)
        header, footer = _extract(sub_html, "header"), _extract(sub_html, "footer")
        for page in pages:
            doc = _document(site, page["title"], page.get("description") or page["brief"],
                            header, mains[page["slug"]], footer)
            _atomic_write(self.root / f"{page['slug']}.html", doc)
        if extra_css.strip():
            merged = css.rstrip() + "\n" + extra_css
            validate_css(merged)
            _atomic_write(css_path, merged)
        backup = self.root / "index.html.before_pages"
        if not backup.exists():
            backup.write_text(html, encoding="utf-8")
        _atomic_write(index_path, apply_plan(html, all_links, final_targets, subpage=False))

        summary = f"Created {len(pages)} pages: " + ", ".join(f"{p['slug']}.html" for p in pages)
        if failed:
            summary += f"; could not create: {', '.join(failed)}"
        if partial:
            summary += "; stopped early at the token budget (run again to add the rest)"
        print(f"[pages] {summary}", flush=True)
        return summary + ". " + self._finish_links()

    def _write_index_only(self, html: str, links: list, targets: dict) -> bool:
        """Apply link targets that need no new page (home, sections, shop). True if anything changed."""
        usable = {lid: t for lid, t in targets.items()
                  if t in ("index", "shop", "cart") or t.startswith("#")}
        if not usable:
            return False
        new = apply_plan(html, links, usable, subpage=False)
        if new == html:
            return False
        backup = self.root / "index.html.before_pages"
        if not backup.exists():
            backup.write_text(html, encoding="utf-8")
        _atomic_write(self.root / "index.html", new)
        return True


def main():
    import sys
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit("usage: python -m studio_core.page_expander PROJECT_ID")
    store = Store()
    project = store.get_project(int(sys.argv[1]))
    if not project:
        raise SystemExit(f"Project #{sys.argv[1]} not found.")
    print(PageExpander(project, store=store).run())


if __name__ == "__main__":
    main()
