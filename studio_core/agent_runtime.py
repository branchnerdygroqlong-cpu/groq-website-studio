"""Runtime agents for Groq Website Studio.

Agents generate; the router retries, continues truncated output and repairs invalid
output. There is no hardcoded website content: if generation fails after retries,
the build fails and nothing is published.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .config import MODELS
from .css_lint import fix_unitless_lengths, missing_utility_rules
from .groq_router import GroqRouter
from .html_assembler import assemble_page, validate_css, validate_fragment
from .storage import Store


def _clean_output(text: str) -> str:
    """Return raw code: take the fenced block if present, dropping any chatter around it."""
    text = (text or "").strip()
    match = re.search(r"```[a-zA-Z0-9]*[ \t]*\n(.*?)(```|\Z)", text, re.S)
    if match:
        text = match.group(1).strip()
    return text


_UNIQUE_TAGS = ("header", "footer", "main", "h1")


def _check_unique(fragment: str, previous: list) -> None:
    earlier = "\n".join(previous)
    for tag in _UNIQUE_TAGS:
        pattern = rf"<{tag}\b"
        if re.search(pattern, fragment, re.I) and re.search(pattern, earlier, re.I):
            raise ValueError(
                f"<{tag}> already exists in another section; this section must not contain it"
            )


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "section"


def _sanitize_fragment(fragment: str, root: Path, anchors: set, own_id: str) -> str:
    """Deterministically REMOVE harmless junk (never adds content), so the model is not
    asked to repair trivial problems: comments, inline styles, missing local images,
    dead links, and a missing/wrong section id."""
    text = re.sub(r"<!--.*?-->", "", fragment, flags=re.S)
    text = re.sub(r"""\sstyle\s*=\s*("[^"]*"|'[^']*')""", "", text, flags=re.I)

    def fix_img(match):
        tag = match.group(0)
        src = re.search(r"""\ssrc\s*=\s*["']([^"']*)["']""", tag, re.I)
        if not src:
            return ""
        value = src.group(1).strip()
        if value.startswith(("http://", "https://", "data:")) or (root / value).is_file():
            return tag
        return ""
    text = re.sub(r"<img\b[^>]*>", fix_img, text, flags=re.I)

    def fix_href(match):
        value = match.group(2).strip()
        if value.startswith(("http://", "https://", "mailto:", "tel:")) or value == "#":
            return match.group(0)
        if value.startswith("#") and value[1:] in anchors:
            return match.group(0)
        return f'{match.group(1)}"#"'
    text = re.sub(r"""(<a\b[^>]*?\shref\s*=\s*)["']([^"']*)["']""", fix_href, text, flags=re.I)

    first = re.search(r"<([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>", text)
    if first and not re.search(rf"""\bid\s*=\s*["']{re.escape(own_id)}["']""", first.group(0)):
        attrs = first.group(2)
        if re.search(r"""\sid\s*=\s*["'][^"']*["']""", attrs):
            attrs = re.sub(r"""(\sid\s*=\s*)["'][^"']*["']""", rf'\1"{own_id}"', attrs, count=1)
        else:
            attrs = f' id="{own_id}"' + attrs
        text = text[:first.start()] + f"<{first.group(1)}{attrs}>" + text[first.end():]
    return text.strip()


_SPECIFIC_PATTERNS = (
    r"\b\d{1,2}:\d{2}\s?(?:am|pm|a\.m\.|p\.m\.)?",      # times / opening hours
    r"\$\s?\d[\d,.]*",                                    # prices
    r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b",            # phone numbers
    r"\b(?:19|20)\d{2}\b",                                 # years
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",                         # email addresses
    r"(?-i:\b\d{1,5}\s+(?:[A-Z][\w'-]*\s+){1,3}(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|"
    r"Boulevard|Blvd|Way|Court|Ct|Place|Pl)\b)",             # street addresses
    r"(?-i:\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b)",               # state + ZIP
)


def _check_invented_specifics(fragment: str, brief: str) -> None:
    """Reject times, prices, phone numbers and years that the brief never gave."""
    visible = re.sub(r"<[^>]+>", " ", fragment)
    allowed = str(brief).lower()
    found = []
    for pattern in _SPECIFIC_PATTERNS:
        for match in re.finditer(pattern, visible, re.I):
            token = match.group(0).strip()
            if "example." in token.lower():      # obvious placeholder domains are fine
                continue
            if token.lower() not in allowed:
                found.append(token)
    if found:
        raise ValueError(
            "invented specifics not given in the brief (" + ", ".join(dict.fromkeys(found))[:80]
            + "); replace them with clearly marked [placeholders]")


_SOCIAL_RE = re.compile(
    r"facebook|twitter|instagram|linkedin|youtube|tiktok|pinterest|snapchat|whatsapp", re.I)


def _check_unrequested_social(fragment: str, brief: str) -> None:
    """Reject social-media links/icons when the brief never mentioned social media."""
    if _SOCIAL_RE.search(brief) or re.search(r"social", brief, re.I):
        return
    for link in re.findall(r"<a\b[^>]*>.*?</a>", fragment, re.S | re.I):
        if _SOCIAL_RE.search(link):
            raise ValueError(
                "social-media links or icons were not requested in the brief; remove them "
                "(do not replace them with other social links)")


def _check_no_js_dependence(fragment: str) -> None:
    """The site has no JavaScript, so hidden panels and toggle buttons can never work."""
    if re.search(r"<[a-z][^>]*\shidden(?:\s|=|>|/)", fragment, re.I):
        raise ValueError(
            "no JavaScript is available, so content with the hidden attribute can never be "
            "opened; use <details><summary> for expandable content such as FAQs")
    if re.search(r"<button\b", fragment, re.I) and not re.search(r"<form\b", fragment, re.I):
        raise ValueError(
            "a <button> outside a form does nothing without JavaScript; use an <a> link, or "
            "<details><summary> for expandable content")


def _ensure_h1(fragments: list):
    """Every page needs exactly one <h1>: promote the first content <h2> if none exists."""
    if any(re.search(r"<h1\b", f, re.I) for f in fragments):
        return fragments, False
    for i, fragment in enumerate(fragments):
        if re.search(r"<(?:header|footer)\b", fragment, re.I):
            continue
        promoted = re.sub(r"<h2\b([^>]*)>(.*?)</h2>", r"<h1\1>\2</h1>", fragment,
                          count=1, flags=re.S | re.I)
        if promoted != fragment:
            return fragments[:i] + [promoted] + fragments[i + 1:], True
    return fragments, False


def _check_links_and_assets(fragment: str, root: Path, anchors: set, own_id: str) -> None:
    """Reject inline styles, missing local images, dead links and missing section id."""
    if re.search(r"\sstyle\s*=", fragment, re.I):
        raise ValueError("inline style attributes are not allowed; style with CSS classes only")
    for src in re.findall(r"<img\b[^>]*?\ssrc\s*=\s*[\"']([^\"']*)[\"']", fragment, re.I):
        src = src.strip()
        if src.startswith(("http://", "https://", "data:")):
            continue
        if not (root / src).is_file():
            raise ValueError(
                f"image file '{src}' does not exist; use only the listed local files, or no <img>"
            )
    for href in re.findall(r"<a\b[^>]*?\shref\s*=\s*[\"']([^\"']*)[\"']", fragment, re.I):
        href = href.strip()
        if href.startswith(("http://", "https://", "mailto:", "tel:")) or href == "#":
            continue
        if href.startswith("#"):
            if href[1:] not in anchors:
                raise ValueError(f"link '{href}' points to a missing anchor; allowed: "
                                 + ", ".join("#" + a for a in sorted(anchors)))
        else:
            raise ValueError(f"link '{href}' is not allowed; this is a one-page site, "
                             "link only to the listed #anchors")
    if not re.search(rf"\bid\s*=\s*[\"']{re.escape(own_id)}[\"']", fragment):
        raise ValueError(f"the outermost element of this section must have id=\"{own_id}\"")


def _finish_reason(response) -> str | None:
    try:
        return response.choices[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return None


def _message(response):
    try:
        return response.choices[0].message
    except (AttributeError, IndexError, TypeError) as exc:
        raise RuntimeError("Groq returned an empty or unrecognized completion.") from exc


def _is_malformed_tool_call_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "failed to parse tool call arguments as json" in message
        or ("tool_use_failed" in message and "failed_generation" in message)
    )


def _validate_build_plan(args):
    if not isinstance(args, dict):
        raise RuntimeError("Project Manager returned a build plan that is not an object.")
    for key in ("image_brief", "site_design_summary", "build_chunks"):
        if key not in args:
            raise RuntimeError(f"Project Manager omitted required field: {key}")
    if not isinstance(args["image_brief"], str) or not args["image_brief"].strip():
        raise RuntimeError("Project Manager returned an empty image_brief.")
    if not isinstance(args["site_design_summary"], str) or not args["site_design_summary"].strip():
        raise RuntimeError("Project Manager returned an empty site_design_summary.")
    if len(args["image_brief"]) > 300:
        raise RuntimeError("Project Manager image_brief exceeded 300 characters.")
    if len(args["site_design_summary"]) > 900:
        raise RuntimeError("Project Manager site_design_summary exceeded 900 characters.")
    chunks = args["build_chunks"]
    if not isinstance(chunks, list) or not 4 <= len(chunks) <= 7:
        raise RuntimeError("Project Manager must return between four and seven homepage sections.")
    for index, chunk in enumerate(chunks, 1):
        if not isinstance(chunk, dict):
            raise RuntimeError(f"Build section {index} is not an object.")
        title = chunk.get("chunk_title")
        instructions = chunk.get("instructions")
        if not isinstance(title, str) or not title.strip() or len(title) > 80:
            raise RuntimeError(f"Build section {index} has an invalid title.")
        if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 500:
            raise RuntimeError(
                f"Build section '{title}' has empty or oversized instructions (maximum 500 characters)."
            )
        try:
            estimate = int(chunk.get("estimated_tokens", 800))
        except (TypeError, ValueError):
            estimate = 800
        chunk["estimated_tokens"] = max(500, min(estimate, 1100))
    return args


def _safe_filename(value):
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(str(value)).stem).strip("-")
    return (name or "image")[:70]


class WebsiteAgents:
    def __init__(self, project, store=None, router=None):
        self.project = project
        self.store = store or Store()
        self.router = router or GroqRouter(self.store)
        self.root = Path(project["root_path"]).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets = self.root / "assets"
        self.assets.mkdir(exist_ok=True)
        self.asset_report = (
            "No local images sourced; use CSS gradients or simple inline SVG where appropriate."
        )

    def _write(self, relative, content, mode="w"):
        target = (self.root / relative).resolve()
        if self.root not in target.parents and target != self.root:
            raise ValueError("Refusing to write outside the project directory.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open(mode, encoding="utf-8") as stream:
            stream.write(content)
        return target

    def source_images(self, brief):
        """Image agent: sources a few freely licensed images from Wikimedia Commons."""
        import requests

        tool_schema = [{
            "type": "function",
            "function": {
                "name": "search_and_download_wikimedia_image",
                "description": (
                    "Search Wikimedia Commons for one freely licensed image and save it "
                    "to the project's assets folder."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string",
                                  "description": "Simple 1-2 word search keyword, e.g. 'sourdough bread'"},
                        "filename": {"type": "string",
                                     "description": "Clean local filename without spaces"},
                    },
                    "required": ["query", "filename"],
                },
            },
        }]
        messages = [
            {"role": "system", "content": (
                "You are an Image Sourcing Specialist. Call "
                "`search_and_download_wikimedia_image` for each requested image (at most "
                "3 images) using simple 1-2 word queries. NEVER retry a failed or "
                "unavailable image: move to the next image, or give a one-line final "
                "summary.")},
            {"role": "user", "content": str(brief)[:3000]},
        ]
        print("[images] Sourcing images from Wikimedia Commons ...", flush=True)
        report = []
        for _ in range(6):
            response = self.router.chat(
                MODELS["image_agent"], messages, tools=tool_schema,
                estimated_out_tokens=700,
                fallback_models=[MODELS["dev_agent"], MODELS["pm"]],
            )
            msg = _message(response)
            messages.append(msg)
            if not getattr(msg, "tool_calls", None):
                if msg.content:
                    report.append(msg.content)
                break
            for call in msg.tool_calls:
                if call.function.name != "search_and_download_wikimedia_image":
                    raise RuntimeError(f"Unexpected image-agent tool: {call.function.name}")
                args = json.loads(call.function.arguments)
                print(f"[images]   searching: {args.get('query', '')}", flush=True)
                result = self._download_wikimedia(
                    requests, args.get("query", ""), args.get("filename", "website-image"))
                print(f"[images]   {result[:150]}", flush=True)
                report.append(result)
                messages.append({
                    "role": "tool", "tool_call_id": call.id,
                    "name": call.function.name, "content": result,
                })
        self.asset_report = "\n".join(report) or "No images downloaded."
        return self.asset_report

    def _download_wikimedia(self, requests, query, filename):
        api = "https://commons.wikimedia.org/w/api.php"
        contact = os.getenv("STUDIO_CONTACT", "local development")
        headers = {"User-Agent": f"GroqWebsiteStudio/0.4 ({contact})"}

        def fetch(url, **kwargs):
            for attempt_no, delay in enumerate((0, 5, 15, 30)):
                if delay:
                    print(f"[images]   Wikimedia busy (429); waiting {delay}s", flush=True)
                    time.sleep(delay)
                reply = requests.get(url, headers=headers, **kwargs)
                if reply.status_code != 429:
                    reply.raise_for_status()
                    return reply
            reply.raise_for_status()
            return reply

        query = str(query).strip()
        words = [w for w in query.split() if len(w) > 2]
        attempts = [query] + ([max(words, key=len)] if len(words) > 1 else [])
        try:
            for attempt in attempts:
                response = fetch(
                    api,
                    params={
                        "action": "query", "format": "json", "generator": "search",
                        "gsrsearch": f"filetype:bitmap {attempt}", "gsrnamespace": 6,
                        "gsrlimit": 8, "prop": "imageinfo",
                        "iiprop": "url|extmetadata", "iiurlwidth": 1200,
                    },
                    timeout=20,
                )
                pages = response.json().get("query", {}).get("pages", {})
                for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
                    info = (page.get("imageinfo") or [{}])[0]
                    meta = info.get("extmetadata", {})
                    license_name = (meta.get("LicenseShortName") or {}).get("value", "")
                    if not license_name:
                        continue  # no identifiable license: skip
                    url = info.get("thumburl") or info.get("url", "")
                    if not url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
                        continue
                    time.sleep(2)  # be polite to upload.wikimedia.org
                    download = fetch(url, timeout=25)
                    name = _safe_filename(filename) + Path(url.split("?")[0]).suffix.lower()
                    (self.assets / name).write_bytes(download.content)
                    page_url = page.get("canonicalurl") or info.get("descriptionurl", "Wikimedia Commons")
                    author = (meta.get("Artist") or {}).get("value", "")
                    author = re.sub(r"<[^>]+>", "", author).strip()[:120]
                    with (self.assets / "CREDITS.txt").open("a", encoding="utf-8") as credits:
                        credits.write(f"{name}: {page_url} | license: {license_name} | author: {author or 'see source'}\n")
                    return (f"SUCCESS: saved as assets/{name} (license: {license_name}). "
                            "Move on to the next image or give your final summary.")
            return (f"UNAVAILABLE: no licensed image found for '{query}'. "
                    "Do NOT retry; move on or finish.")
        except Exception as exc:
            return f"ERROR: {exc}. Do NOT retry; move on."

    # gpt-oss-20b first (cheap, obeys format), gpt-oss-120b next, Qwen last (verbose).
    def _worker_models(self):
        return [MODELS["dev_fallback"], MODELS["pm"], MODELS["dev_agent"]]

    _CSS_STYLE_GUIDE = (
        "Aim for a polished, modern, professional look: a consistent spacing scale and "
        "type scale, generous whitespace, a restrained palette from the design notes, "
        "max-width centered containers, flexbox/grid layouts (auto-fit minmax grids), "
        "cards with radius and subtle shadow, buttons with padding/radius/hover states, "
        "images with object-fit: cover and a fixed aspect-ratio, hero text overlaid with "
        "position: relative/absolute only if the markup has an overlay, mobile-first with "
        "a min-width media query for larger screens. A section's parts (heading, intro "
        "text, list, media) stack vertically by default: use side-by-side columns only "
        "for 2 to 3 clearly parallel items such as cards, or an image beside text. Every "
        "top-level section has at least 4rem of vertical padding and is visually "
        "separated from its neighbours by a background tone change or a thin border. "
        "Output valid CSS only."
    )

    def _css_call(self, label, user_text):
        messages = [
            {"role": "system", "content": (
                "You are an expert CSS designer. Return only complete CSS. No HTML, "
                "Markdown or explanations. " + self._CSS_STYLE_GUIDE)},
            {"role": "user", "content": user_text},
        ]
        css_part, _model = self.router.generate(
            self._worker_models(), messages, validate_css,
            estimated_out_tokens=1200, label=label, clean=_clean_output,
        )
        return css_part

    def _generate_css(self, design_summary, fragments, titles=None):
        """HTML is built first; CSS is then written per section from its real markup."""
        titles = titles or [f"Section {i}" for i in range(1, len(fragments) + 1)]
        all_html = "\n".join(fragments)
        tags = ", ".join(sorted(set(re.findall(r"<([a-z][a-z0-9]*)", all_html))))[:400]

        foundation = self._css_call("CSS foundation", (
            f"Website: {self.project['name']}\nDesign notes: {design_summary[:900]}\n"
            f"HTML tags used on the page: {tags}\n\n"
            "Write the FOUNDATION: :root design tokens (colors, font stacks, spacing scale, "
            "radius, shadow), box-sizing reset, body, headings scale, paragraphs, links, "
            "img, buttons and form-control base styles, and both a .visually-hidden and a "
            ".sr-only utility class. Every spacing, size and radius variable MUST include "
            "a unit (for example --space-4: 1rem), never a bare number."))
        variables = ", ".join(dict.fromkeys(re.findall(r"--[a-zA-Z0-9_-]+", foundation)))[:600]

        parts = [f"\n/* FOUNDATION */\n{foundation}"]
        for title, fragment in zip(titles, fragments):
            css_part = self._css_call(f"CSS {title}", (
                f"Website: {self.project['name']}\nDesign notes: {design_summary[:600]}\n"
                f"Existing CSS variables (use them; do not redefine :root): {variables}\n\n"
                f"Section '{title}' HTML:\n{fragment[:3000]}\n\n"
                "Write CSS ONLY for the classes, ids and elements in this HTML: layout, "
                "spacing, typography, colors, images, hover/focus states, and the "
                "responsive behavior for this section."))
            parts.append(f"\n/* {title} */\n{css_part}")

        parts.append("\n/* ACCESSIBILITY */\n" + self._css_call("CSS accessibility", (
            f"Existing CSS variables: {variables}\n"
            "Write only: visible :focus-visible styles for links, buttons and form "
            "controls, prefers-reduced-motion rules, and readable minimum text sizes.")))
        css = "\n".join(parts)
        validate_css(css)
        return css

    def _add_image_credits(self):
        """Add an 'Image credits' section for the downloaded images the site really uses."""
        from html import escape
        credits_file = self.assets / "CREDITS.txt"
        index, css_file = self.root / "index.html", self.root / "styles.css"
        if not credits_file.is_file() or not index.is_file():
            return 0
        html = index.read_text(encoding="utf-8")
        css = css_file.read_text(encoding="utf-8") if css_file.is_file() else ""
        html = re.sub(r'<section id="image-credits".*?</section>\s*', "", html, flags=re.S)
        used = set(re.findall(r"assets/([^\"'()\s<>]+)", html + css))
        items = []
        for line in credits_file.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^(.+?): (\S+) \| license: (.*?) \| author: (.*)$", line.strip())
            if not m or m.group(1) not in used:
                continue
            name, source, licence, author = m.groups()
            label = escape(name.rsplit(".", 1)[0].replace("_", " ").replace("-", " "))
            link = (f'<a href="{escape(source, quote=True)}" rel="noopener">{label}</a>'
                    if source.startswith(("http://", "https://")) else label)
            items.append(f"    <li>{link} - {escape(author)}, {escape(licence)}</li>")
        if not items:
            return 0
        block = ('<section id="image-credits" class="image-credits" aria-labelledby="image-credits-title">\n'
                 '  <h2 id="image-credits-title">Image credits</h2>\n  <ul>\n'
                 + "\n".join(items) + "\n  </ul>\n</section>\n")
        self._write("index.html", html.replace("</body>", block + "</body>", 1))
        if ".image-credits" not in css:
            self._write("styles.css", css.rstrip() + (
                "\n\n/* IMAGE CREDITS */\n.image-credits{padding:1.5rem 5%;font-size:.85rem;opacity:.85}\n"
                ".image-credits h2{font-size:1rem;margin:0 0 .5rem}\n"
                ".image-credits ul{margin:0;padding-left:1.2rem}\n"))
        return len(items)

    def build_page(self, design_summary, chunks, brief=""):
        if not isinstance(chunks, list) or not 1 <= len(chunks) <= 12:
            raise ValueError("The Project Manager must provide between 1 and 12 section chunks.")

        titles_all = [str(c.get("chunk_title", f"Section {i}"))[:160]
                      for i, c in enumerate(chunks, 1) if isinstance(c, dict)]
        fragments, section_titles = [], []
        slugs = [_slug(t) for t in titles_all]
        anchors = set(slugs)
        local_files = sorted(
            str(f.relative_to(self.root)) for f in self.assets.rglob("*") if f.is_file()
        )

        for index, chunk in enumerate(chunks, 1):
            if not isinstance(chunk, dict):
                raise ValueError(f"Build chunk {index} is not an object.")
            title = titles_all[index - 1]
            instructions = str(chunk.get("instructions", ""))[:4500]
            if not instructions.strip():
                raise ValueError(f"Build chunk '{title}' has no instructions.")
            others = [t for t in titles_all if t != title]
            own_id = slugs[index - 1]

            messages = [
                {"role": "system", "content": (
                    "You are a senior frontend developer. Return ONLY the raw HTML for ONE "
                    "section: no Markdown fences, no apology or commentary, no HTML comments, "
                    "no doctype/html/head/body/style/script/title/meta/link tags. Use "
                    "semantic accessible HTML with descriptive class names, properly nested "
                    "tags, and real written content (no empty sections or 'details here' "
                    "stubs). Do NOT draw inline SVG. Do not invent testimonials, statistics, "
                    "prices, addresses, opening hours, years or other facts: use clearly "
                    "marked [placeholders]. Do not add forms, quote or order requests, newsletter "
                    "signups, or social-media links unless this section's requirements "
                    "explicitly ask for them. The page has no JavaScript: for expandable content such "
                    "as an FAQ use <details> and <summary>, never the hidden attribute or "
                    "toggle buttons. Use relative paths for local assets.")},
                {"role": "user", "content": (
                    f"Project: {self.project['name']}\n"
                    f"Design: {design_summary[:1800]}\n"
                    f"Assets: {str(self.asset_report)[:1000]}\n"
                    f"Available local image files (the ONLY images you may use; if none, "
                    f"use no <img>): {', '.join(local_files) or 'none'}\n"
                    f"Page anchors (the ONLY internal links allowed): "
                    f"{', '.join('#' + a for a in slugs)}\n"
                    f"The outermost element of THIS section must have id=\"{own_id}\". "
                    "Never use inline style attributes.\n"
                    f"Section {index} of {len(chunks)}: {title}\n"
                    f"Requirements: {instructions}\n"
                    f"Write ONLY this section. Other sections exist separately; do NOT "
                    f"include any of them: {', '.join(others) or 'none'}. Use at most one "
                    "<h1> on the whole site (only if this is the hero/intro), and "
                    "<header>/<footer> only if this section is the site header/footer. "
                    "If this is the first section after the site header (the intro or hero), "
                    "its main heading must be the page's single <h1>.")},
            ]

            def validate(text, t=title, prior=list(fragments), oid=own_id):
                validate_fragment(text, t)
                _check_unique(text, prior)
                _check_links_and_assets(text, self.root, anchors, oid)
                _check_invented_specifics(text, brief)
                _check_unrequested_social(text, brief)
                _check_no_js_dependence(text)

            fragment, _model = self.router.generate(
                self._worker_models(), messages, validate,
                estimated_out_tokens=1000, label=f"HTML {title}",
                clean=lambda raw, oid=own_id: _sanitize_fragment(
                    _clean_output(raw), self.root, anchors, oid),
            )
            fragments.append(fragment)
            section_titles.append(title)

        fragments, promoted = _ensure_h1(fragments)
        if promoted:
            print("[fix] The page had no <h1>; promoted the first content heading.", flush=True)
        css = self._generate_css(design_summary, fragments, section_titles)
        css, css_notes = fix_unitless_lengths(css)
        for note in css_notes:
            print(f"[css-fix] {note}", flush=True)
        utilities = missing_utility_rules("\n".join(fragments), css)
        if utilities:
            css += "\n\n/* UTILITIES */\n" + utilities + "\n"
            print("[css-fix] added missing visually-hidden utility class(es).", flush=True)
        validate_css(css)
        desc = " ".join(str(brief or "").split())[:160] or f"{self.project['name']} website"
        output = assemble_page(
            project_name=self.project["name"], css=css, fragments=fragments,
            output_dir=self.root, description=desc,
        )
        credited = self._add_image_credits()
        if credited:
            print(f"[credits] Added visible credits for {credited} image(s).", flush=True)
        return (
            f"Built and validated {len(fragments)} HTML sections into {output}; "
            f"stylesheet: {self.root / 'styles.css'}; "
            f"sections: {', '.join(section_titles)}"
        )

    def run(self, brief):
        """Plan, source optional images, generate sections, assemble, and validate."""
        task_id = self.store.add_task(
            self.project["id"], "Plan and build website", brief, "project_manager"
        )
        self.store.update_task(task_id, "running")
        if hasattr(self.router, "reset_budget"):
            self.router.reset_budget()
        pm_tools = [{
            "type": "function",
            "function": {
                "name": "delegate_website_build",
                "description": (
                    "Return one compact homepage plan. Keep all descriptions brief; "
                    "the frontend agent writes the actual HTML."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_brief": {
                            "type": "string", "maxLength": 300,
                            "description": "One short image-search description, at most 300 characters.",
                        },
                        "site_design_summary": {
                            "type": "string", "maxLength": 900,
                            "description": "Compact visual direction and palette, at most 900 characters.",
                        },
                        "build_chunks": {
                            "type": "array", "minItems": 4, "maxItems": 7,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "chunk_title": {"type": "string", "maxLength": 80},
                                    "instructions": {
                                        "type": "string", "maxLength": 500,
                                        "description": "One or two concise sentences; no HTML code or CSS.",
                                    },
                                    "estimated_tokens": {
                                        "type": "integer", "minimum": 500, "maximum": 1100,
                                    },
                                },
                                "required": ["chunk_title", "instructions", "estimated_tokens"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["image_brief", "site_design_summary", "build_chunks"],
                    "additionalProperties": False,
                },
            },
        }]
        system_prompt = (
            "You are the project manager for a professional website studio. "
            "Call delegate_website_build exactly once. Plan ONE complete homepage. First "
            "decide from the brief what KIND of site it is (business, personal page, "
            "portfolio, blog or article site, hobby or community, event, nonprofit, ...) "
            "and plan sections that fit that kind of site and the brief's own content. "
            "Never turn a personal or informational site into a business that sells "
            "services, and never invent a company, services, or products the brief does "
            "not mention. Choose 4 to 7 ordered sections with unique, descriptive titles. "
            "Section 1 must be the site header with navigation only; the last section must "
            "be the footer, containing navigation links and placeholders only. Plan a "
            "quote request, contact or order form, booking, newsletter signup, pricing, or "
            "any sales call to action ONLY if the brief asks for it; otherwise do not plan "
            "one. Keep image_brief under 300 characters and site_design_summary under 900. "
            "Each section instruction must be one or two short sentences, under 500 "
            "characters. Describe content and layout only: no HTML, CSS or long lists. "
            "The assembler creates the document wrapper and the frontend agent writes each "
            "section. Do not invent prices, testimonials, awards, addresses, hours, "
            "certifications, or business facts; use clearly marked [placeholders]."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": str(brief)[:6000]},
        ]

        try:
            self.store.set_project_status(self.project["id"], "building")
            try:
                response = self.router.chat(
                    MODELS["pm"], messages, tools=pm_tools, estimated_out_tokens=1800,
                    fallback_models=[MODELS["dev_agent"], MODELS["image_agent"]],
                )
            except Exception as first_exc:
                if not _is_malformed_tool_call_error(first_exc):
                    raise
                retry_messages = [
                    {"role": "system", "content": (
                        "Call delegate_website_build exactly once. Output only a compact "
                        "homepage plan of 4 to 7 sections that fit the kind of site the brief "
                        "describes: first is the header/navigation, last is the footer. No "
                        "quote, contact-form, order or sales sections unless the brief asks. "
                        "Use very short phrases. Keep image_brief <=300 chars, design summary <=900 chars, "
                        "and each section instruction <=500 chars. No HTML or CSS.")},
                    {"role": "user", "content": str(brief)[:2500]},
                ]
                try:
                    response = self.router.chat(
                        MODELS["pm"], retry_messages, tools=pm_tools, estimated_out_tokens=1800,
                        fallback_models=[MODELS["dev_agent"], MODELS["image_agent"]],
                    )
                except Exception as retry_exc:
                    if _is_malformed_tool_call_error(retry_exc):
                        raise RuntimeError(
                            "Groq twice returned an incomplete Project Manager tool call. "
                            "The plan was not accepted and no page was published."
                        ) from retry_exc
                    raise

            if _finish_reason(response) == "length":
                raise RuntimeError(
                    "Project Manager plan was truncated by Groq's output-token limit. "
                    "No page was published."
                )
            msg = _message(response)
            calls = getattr(msg, "tool_calls", None) or []
            if len(calls) != 1 or calls[0].function.name != "delegate_website_build":
                raise RuntimeError(
                    "Project Manager must dispatch delegate_website_build exactly once. "
                    "No page was published."
                )
            try:
                args = json.loads(calls[0].function.arguments)
            except (TypeError, json.JSONDecodeError) as parse_exc:
                raise RuntimeError(
                    "Project Manager returned invalid JSON arguments. No page was published."
                ) from parse_exc
            args = _validate_build_plan(args)

            image_task = self.store.add_task(
                self.project["id"], "Source website images", args["image_brief"], "image_sourcer"
            )
            self.store.update_task(image_task, "running")
            try:
                image_result = self.source_images(args["image_brief"])
                self.store.update_task(image_task, "completed", image_result)
                have = sorted(f.name for f in self.assets.iterdir() if f.is_file() and f.name != "CREDITS.txt")
                print(f"[images] done; local images: {', '.join(have) or 'none'}", flush=True)
            except Exception as image_exc:
                self.asset_report = (
                    "Image sourcing was unavailable; use CSS gradients or inline SVG. "
                    f"Details: {image_exc}"
                )
                self.store.update_task(image_task, "failed", str(image_exc))
                print(f"[images] image sourcing failed: {image_exc}", flush=True)

            frontend_task = self.store.add_task(
                self.project["id"], "Generate and assemble homepage",
                args["site_design_summary"], "frontend_developer",
            )
            self.store.update_task(frontend_task, "running")
            try:
                result = self.build_page(args["site_design_summary"], args["build_chunks"], brief)
                self.store.update_task(frontend_task, "completed", result)
            except Exception as frontend_exc:
                self.store.update_task(frontend_task, "failed", str(frontend_exc))
                raise

            report = (
                f"Project: {self.project['name']}\n"
                f"{result}\n\nImage report:\n{self.asset_report}\n"
            )
            self._write("BUILD_REPORT.txt", report)
            self.store.update_task(task_id, "completed", result)
            self.store.set_project_status(self.project["id"], "built")
            return result
        except Exception as exc:
            self.store.update_task(task_id, "failed", str(exc))
            self.store.set_project_status(self.project["id"], "build_failed")
            raise
