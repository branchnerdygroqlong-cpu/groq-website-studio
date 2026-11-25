"""Finishing touches that are added to a built site WITHOUT any AI model.

  * contact email and social-media links (from the Studio form) in every page's footer
  * every page links back to the main page (logo, a "Back to the main page" link, and
    section links that point at index.html#section)
  * the optional shopping cart (shop_cart.py in the project root)

Because these are written by plain code, they are exact: the model never sees, retypes or
invents an email address or handle. Running finalize() again replaces the previous
version of each block, so nothing is duplicated.

Contacts and the shop are separate choices: set_contacts / remove_contacts and set_shop /
remove_shop each change only their own part.

Settings live in <site folder>/site_settings.json:
  {"email": "me@example.com", "social": {"instagram": "mypage"}, "shop": {"enabled": true}}
"""
from __future__ import annotations

import html
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote, urlparse

SETTINGS_FILE = "site_settings.json"
EXTRAS_CSS_MARKER = "/* STUDIO-EXTRAS */"

# key -> (label, link template for a bare handle, domains accepted when a full link is pasted)
SOCIAL = {
    "facebook": ("Facebook", "https://www.facebook.com/{h}", ("facebook.com", "fb.com", "fb.me")),
    "instagram": ("Instagram", "https://www.instagram.com/{h}", ("instagram.com",)),
    "x": ("X", "https://x.com/{h}", ("x.com", "twitter.com")),
    "tiktok": ("TikTok", "https://www.tiktok.com/@{h}", ("tiktok.com",)),
    "youtube": ("YouTube", "https://www.youtube.com/@{h}", ("youtube.com", "youtu.be")),
    "linkedin": ("LinkedIn", "https://www.linkedin.com/in/{h}", ("linkedin.com",)),
}
_HANDLE = re.compile(r"^[A-Za-z0-9._-]{1,60}$")
_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+$")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def normalize_settings(raw):
    """Validate what the form sent. Returns (clean_settings, warnings). Anything invalid is
    dropped with a warning rather than written into the site."""
    raw = raw if isinstance(raw, dict) else {}
    clean, warnings = {"email": "", "social": {}, "shop": {"enabled": False}}, []

    email = str(raw.get("email", "")).strip()
    if email:
        if _EMAIL.match(email) and len(email) <= 120:
            clean["email"] = email
        else:
            warnings.append(f"'{email[:60]}' is not a valid email address; contact email skipped.")

    social = raw.get("social") if isinstance(raw.get("social"), dict) else {}
    for key, (label, template, domains) in SOCIAL.items():
        value = str(social.get(key, "")).strip()
        if not value:
            continue
        if re.match(r"^[a-z][a-z0-9+.-]*:", value, re.I) or value.lower().startswith("www.") \
                or "/" in value:
            candidate = value if re.match(r"^https?://", value, re.I) else "https://" + value.lstrip("/")
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").lower()
            ok = parsed.scheme in ("http", "https") and any(host == d or host.endswith("." + d) for d in domains)
            if ok and not parsed.username:
                clean["social"][key] = parsed._replace(scheme="https", fragment="").geturl()
            else:
                warnings.append(f"{label}: '{value[:60]}' is not a {label} link; skipped.")
            continue
        handle = value.lstrip("@")
        if _HANDLE.match(handle):
            clean["social"][key] = template.format(h=quote(handle))
        else:
            warnings.append(f"{label}: '{value[:60]}' is not a valid handle; skipped.")

    shop = raw.get("shop")
    clean["shop"]["enabled"] = bool(shop.get("enabled")) if isinstance(shop, dict) else bool(shop)
    return clean, warnings


def load_settings(folder):
    try:
        data = json.loads((Path(folder) / SETTINGS_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(folder, clean):
    _write(Path(folder) / SETTINGS_FILE, json.dumps(clean, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    os.replace(tmp, path)


def _pages(folder: Path):
    return sorted(p for p in Path(folder).glob("*.html") if not p.name.startswith("."))


def _ids(page: str) -> set:
    return set(re.findall(r"""\sid\s*=\s*["']([^"']+)["']""", page))


def _strip_block(page: str, name: str) -> str:
    return re.sub(rf"\s*<!-- studio-{name}:start -->.*?<!-- studio-{name}:end -->\s*", "", page, flags=re.S)


def _insert_before(page: str, closing: str, block: str) -> str:
    """Put `block` just before the last `closing` tag, normalising the whitespace around it
    so that running the same insert twice gives byte-identical files."""
    idx = page.lower().rfind(closing)
    return page[:idx].rstrip() + "\n" + block + "\n" + page[idx:]


def _insert_after(page: str, end: int, block: str) -> str:
    return page[:end].rstrip() + "\n" + block + "\n" + page[end:].lstrip()


EXTRAS_CSS = f"""
{EXTRAS_CSS_MARKER}
.site-extras{{display:flex;flex-wrap:wrap;gap:.75rem 2rem;align-items:center;justify-content:center;padding:1.5rem 5%}}
.site-social{{list-style:none;display:flex;flex-wrap:wrap;gap:.5rem 1.25rem;margin:0;padding:0}}
.site-social a,.site-contact a{{color:inherit;text-decoration:underline;text-underline-offset:.2em}}
.site-contact{{margin:0}}
.back-link{{margin:0 0 1.5rem}}
.back-link a{{color:inherit;text-underline-offset:.2em}}
"""


# --------------------------------------------------------------------------- #
# Contact and social links
# --------------------------------------------------------------------------- #
def _extras_block(settings: dict) -> str:
    parts = []
    social = settings.get("social") or {}
    items = []
    for key, (label, _template, _domains) in SOCIAL.items():
        if social.get(key):
            items.append(f'<li><a href="{html.escape(social[key], quote=True)}" '
                         f'rel="noopener noreferrer" target="_blank">{label}</a></li>')
    if items:
        parts.append('  <ul class="site-social" aria-label="Social media">\n    '
                     + "\n    ".join(items) + "\n  </ul>")
    email = settings.get("email")
    if email:
        safe = html.escape(email, quote=True)
        parts.append(f'  <p class="site-contact">Contact: <a href="mailto:{safe}">{html.escape(email)}</a></p>')
    if not parts:
        return ""
    return ('<!-- studio-extras:start -->\n<div class="site-extras">\n' + "\n".join(parts)
            + "\n</div>\n<!-- studio-extras:end -->")


def apply_contact_social(folder, settings) -> str:
    folder = Path(folder)
    block = _extras_block(settings)
    touched = 0
    for path in _pages(folder):
        page = _strip_block(path.read_text(encoding="utf-8"), "extras")
        if block:
            if re.search(r"</footer>", page, re.I):
                page = _insert_before(page, "</footer>", block)
            elif re.search(r"</body>", page, re.I):
                page = _insert_before(page, "</body>", block)
            else:
                page = page.rstrip() + "\n" + block + "\n"
        _write(path, page)
        touched += 1
    css_path = folder / "styles.css"
    if block and css_path.is_file():
        css = css_path.read_text(encoding="utf-8")
        if EXTRAS_CSS_MARKER not in css:
            _write(css_path, css.rstrip() + "\n" + EXTRAS_CSS)
    kinds = []
    if settings.get("social"):
        kinds.append(f"{len(settings['social'])} social link(s)")
    if settings.get("email"):
        kinds.append("contact email")
    return (f"added {' and '.join(kinds)} to {touched} page(s)" if kinds
            else f"no contact or social details to add ({touched} page(s) cleaned)")


# --------------------------------------------------------------------------- #
# Every page links back to the main page
# --------------------------------------------------------------------------- #
def ensure_home_links(folder) -> str:
    """The logo goes to the main page, every other page opens with a 'Back to the main page'
    link, and section links on other pages point at index.html#section."""
    folder = Path(folder)
    index = folder / "index.html"
    if not index.is_file():
        return "no index.html; skipped"
    index_ids = _ids(index.read_text(encoding="utf-8"))
    fixed = 0
    for path in _pages(folder):
        page = path.read_text(encoding="utf-8")
        original = page
        is_index = path.name == "index.html"
        header = re.search(r"<header\b.*?</header>", page, re.S | re.I)
        if header:                                  # the logo / site-name link (first link in the header)
            block = header.group(0)
            first = re.search(r"""<a\b[^>]*?\shref\s*=\s*(["'])(.*?)\1""", block, re.S | re.I)
            if first and first.group(2).strip() in ("", "#"):
                new = block[:first.start(2)] + "index.html" + block[first.end(2):]
                page = page[:header.start()] + new + page[header.end():]
        if not is_index:
            own = _ids(page)

            def to_index(match):
                name = match.group(2)
                if name in own:
                    return match.group(0)
                target = f"index.html#{name}" if name in index_ids else "index.html"
                return f'{match.group(1)}"{target}"'
            page = re.sub(r"""(<a\b[^>]*?\shref\s*=\s*)["']#([A-Za-z][^"']*)["']""", to_index, page, flags=re.I)
            page = _strip_block(page, "back")
            if 'class="back-link"' not in page:
                back = ('<!-- studio-back:start -->\n<p class="back-link"><a href="index.html">'
                        '&larr; Back to the main page</a></p>\n<!-- studio-back:end -->')
                main = re.search(r"<main\b[^>]*>", page, re.I)
                if main:
                    page = _insert_after(page, main.end(), back)
                elif re.search(r"</header>", page, re.I):
                    page = _insert_after(page, page.lower().find("</header>") + len("</header>"), back)
        if page != original:
            _write(path, page)
            fixed += 1
    css_path = folder / "styles.css"
    if css_path.is_file():
        css = css_path.read_text(encoding="utf-8")
        if ".back-link" not in css:
            _write(css_path, css.rstrip() + "\n.back-link{margin:0 0 1.5rem}\n.back-link a{color:inherit;text-underline-offset:.2em}\n")
    return f"home links checked on {len(_pages(folder))} page(s); updated {fixed}"


# --------------------------------------------------------------------------- #
# The separate actions: contacts and shop are independent choices
# --------------------------------------------------------------------------- #
def _site(folder) -> Path:
    folder = Path(folder).resolve()
    if not (folder / "index.html").is_file():
        raise RuntimeError(f"{folder.name} has no index.html yet.")
    return folder


def _contact_part(raw):
    """Validated email and social links from the form: (email, social, warnings)."""
    clean, warnings = normalize_settings({"email": (raw or {}).get("email", ""),
                                          "social": (raw or {}).get("social") or {}})
    return clean["email"], clean["social"], warnings


def set_contacts(folder, raw, log=print):
    """Add (or replace) the contact email and social links. The shop is not touched."""
    folder = _site(folder)
    email, social, warnings = _contact_part(raw)
    for warning in warnings:
        log(f"[contacts] {warning}")
    if not email and not social:
        raise RuntimeError("Enter a contact email or at least one valid social media handle or link.")
    saved = load_settings(folder)
    saved["email"], saved["social"] = email, social
    saved.setdefault("shop", {"enabled": False})
    save_settings(folder, saved)
    log("[contacts] " + apply_contact_social(folder, saved))
    log("[contacts] " + ensure_home_links(folder))


def remove_contacts(folder, log=print):
    folder = _site(folder)
    saved = load_settings(folder)
    saved["email"], saved["social"] = "", {}
    saved.setdefault("shop", {"enabled": False})
    save_settings(folder, saved)
    log("[contacts] " + apply_contact_social(folder, saved))


def set_shop(folder, log=print):
    """Add or refresh the shopping cart from items.csv. Contact details are not touched."""
    folder = _site(folder)
    try:
        import shop_cart                          # lives in the project root, next to studio_gui.py
    except ImportError:
        raise RuntimeError("shop_cart.py was not found in the project root, next to studio_gui.py.")
    saved = load_settings(folder)
    saved["shop"] = {"enabled": True}
    saved.setdefault("email", "")
    saved.setdefault("social", {})
    save_settings(folder, saved)
    for line in shop_cart.build_shop(folder):
        log(line)
    log(f"[shop] Edit your items and prices in {folder / 'items.csv'}, then press 'Add shopping cart' again.")
    log("[shop] " + ensure_home_links(folder))


def remove_shop(folder, log=print):
    folder = _site(folder)
    try:
        import shop_cart
    except ImportError:
        raise RuntimeError("shop_cart.py was not found in the project root, next to studio_gui.py.")
    saved = load_settings(folder)
    saved["shop"] = {"enabled": False}
    saved.setdefault("email", "")
    saved.setdefault("social", {})
    save_settings(folder, saved)
    for line in shop_cart.remove_shop(folder):
        log(line)


def finalize(folder, options, log=print):
    """Used right after a build. `options` is what the form sent:
    {"contacts": bool, "email": ..., "social": {...}, "shop": {"enabled": bool}}.
    Only the chosen parts are added; nothing is removed. Home links are always checked."""
    folder = _site(folder)
    options = options if isinstance(options, dict) else {}
    if options.get("contacts"):
        try:
            set_contacts(folder, options, log)
        except RuntimeError as exc:
            log(f"[contacts] {exc}")
    shop = options.get("shop")
    if (shop.get("enabled") if isinstance(shop, dict) else shop):
        try:
            set_shop(folder, log)
        except RuntimeError as exc:
            log(f"[shop] {exc}")
    log("[extras] " + ensure_home_links(folder))
