#!/usr/bin/env python3
"""A blog for a generated site, kept as its own part of the website.

    blog.html            the list of posts (newest first)
    blog-<post>.html     one page per published post
    feed.xml             an RSS feed (when the website's public address is known)
Posts are written as blocks (heading, text, picture, button, divider) and stored in <site>/.blog/posts.json
(a hidden folder that is never served). Pages are rebuilt from that file, so a draft never appears on the site.
Standard library only.
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import tempfile
from datetime import date, datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import site_editor

DIR = ".blog"
MARK = "<!-- studio-blog-page -->"
CSS_MARK = "/* STUDIO-BLOG */"
MAX_POSTS, MAX_BLOCKS, MAX_TEXT = 500, 80, 30000


class BlogError(ValueError):
    pass


def _file(folder) -> Path:
    d = Path(folder) / DIR
    d.mkdir(exist_ok=True)
    return d / "posts.json"


def load(folder) -> list:
    try:
        data = json.loads(_file(folder).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(folder, posts) -> None:
    path = _file(folder)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(posts, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _site_url(folder) -> str:
    try:
        import newsletter
        return newsletter.load_settings(folder)["site_url"]
    except Exception:
        return ""


def _safe_src(value: str, label="picture") -> str:
    value = str(value or "").strip()
    if not value or re.search(r"[\x00-\x20]", value) or len(value) > 2000:
        raise BlogError(f"The {label} address is missing or has spaces.")
    if re.match(r"^https?://", value, re.I):
        return value
    if re.match(r"^[a-z][a-z0-9+.-]*:", value, re.I) or value.startswith(("/", "\\")) or ".." in value.split("/"):
        raise BlogError(f"The {label} must be a picture from this site (like assets/photo.jpg) or an https:// address.")
    return value


def _safe_url(value: str) -> str:
    value = str(value or "").strip()
    if not re.match(r"^(https?://|mailto:)", value, re.I) and not re.match(r"^[A-Za-z0-9_./#-]+$", value):
        raise BlogError("Links must start with https:// or point to a page on this site.")
    if re.search(r"[\x00-\x20]", value) or len(value) > 2000 or re.match(r"^\s*javascript:", value, re.I):
        raise BlogError("A link contains characters that are not allowed.")
    return value


def clean_blocks(blocks) -> list:
    if not isinstance(blocks, list) or not blocks:
        raise BlogError("Write something first: add at least one block.")
    if len(blocks) > MAX_BLOCKS:
        raise BlogError(f"A post can have at most {MAX_BLOCKS} blocks.")
    out = []
    for b in blocks:
        b = b if isinstance(b, dict) else {}
        kind = b.get("type")
        if kind in ("heading", "text"):
            text = str(b.get("text", "")).replace("\r\n", "\n").strip()
            if not text:
                raise BlogError("A heading or text block is empty. Fill it in or remove it.")
            out.append({"type": kind, "text": text[:MAX_TEXT]})
        elif kind == "image":
            out.append({"type": "image", "src": _safe_src(b.get("src")), "alt": " ".join(str(b.get("alt", "")).split())[:200],
                        "caption": " ".join(str(b.get("caption", "")).split())[:300]})
        elif kind == "button":
            text = " ".join(str(b.get("text", "")).split())[:60]
            if not text:
                raise BlogError("A button needs its text.")
            out.append({"type": "button", "text": text, "url": _safe_url(b.get("url"))})
        elif kind == "divider":
            out.append({"type": "divider"})
        else:
            raise BlogError("Unknown block type.")
    return out


def _slug(title, used) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50].strip("-") or "post"
    slug, n = base, 2
    while slug in used:
        slug, n = f"{base}-{n}", n + 1
    return slug


def save_post(folder, data: dict) -> dict:
    posts = load(folder)
    title = " ".join(str(data.get("title", "")).split())[:150]
    if not title:
        raise BlogError("Enter a title for the post.")
    day = str(data.get("date") or date.today().isoformat())
    try:
        date.fromisoformat(day)
    except ValueError:
        raise BlogError("Enter the date like 2026-11-24.")
    status = data.get("status")
    if status not in ("draft", "published"):
        raise BlogError("Choose draft or published.")
    cover = str(data.get("cover", "")).strip()
    post = {"title": title, "date": day, "status": status, "blocks": clean_blocks(data.get("blocks")),
            "excerpt": " ".join(str(data.get("excerpt", "")).split())[:300],
            "cover": _safe_src(cover, "cover picture") if cover else ""}
    pid = str(data.get("id") or "")
    existing = next((p for p in posts if p["id"] == pid), None) if pid else None
    if existing:
        existing.update(post)
        post = existing
    else:
        if len(posts) >= MAX_POSTS:
            raise BlogError("That is the most posts a blog can hold.")
        post["id"] = secrets.token_hex(4)
        post["slug"] = _slug(title, {p["slug"] for p in posts})
        posts.append(post)
    _save(folder, posts)
    return post


def delete_post(folder, pid) -> None:
    posts = [p for p in load(folder) if p["id"] != pid]
    _save(folder, posts)


def _excerpt(post) -> str:
    if post["excerpt"]:
        return post["excerpt"]
    first = next((b["text"] for b in post["blocks"] if b["type"] == "text"), "")
    first = " ".join(first.split())
    return first if len(first) <= 200 else first[:200].rsplit(" ", 1)[0] + "…"


def post_file(post) -> str:
    return f"blog-{post['slug']}.html"


def _pretty(day: str) -> str:
    d = date.fromisoformat(day)
    return d.strftime("%B ") + str(d.day) + d.strftime(", %Y")


def _body(post) -> str:
    out = []
    for b in post["blocks"]:
        if b["type"] == "heading":
            out.append(f'    <h2>{html.escape(b["text"])}</h2>')
        elif b["type"] == "text":
            out.extend("    <p>" + html.escape(p).replace("\n", "<br>") + "</p>"
                       for p in re.split(r"\n\s*\n", b["text"]) if p.strip())
        elif b["type"] == "image":
            cap = f'<figcaption>{html.escape(b["caption"])}</figcaption>' if b["caption"] else ""
            out.append(f'    <figure class="blog-figure"><img src="{html.escape(b["src"], quote=True)}" '
                       f'alt="{html.escape(b["alt"], quote=True)}" loading="lazy">{cap}</figure>')
        elif b["type"] == "button":
            out.append(f'    <p><a class="blog-button" href="{html.escape(b["url"], quote=True)}">{html.escape(b["text"])}</a></p>')
        else:
            out.append("    <hr>")
    return "\n".join(out)


def _write_page(folder, name, title, main, extra_head=""):
    head, hdr, ftr = site_editor.page_shell(folder, title, extra_head=extra_head)
    page = f'<!doctype html>\n<html lang="en">\n<head>\n{head}\n</head>\n<body>\n{MARK}\n{hdr}\n{main}\n{ftr}\n</body>\n</html>\n'
    site_editor._write(Path(folder) / name, page)


def rebuild(folder) -> list:
    """Regenerate blog.html, one page per published post, and feed.xml. Drafts never appear."""
    folder = Path(folder).resolve()
    posts = sorted([p for p in load(folder) if p["status"] == "published"],
                   key=lambda p: (p["date"], p["id"]), reverse=True)
    keep = {post_file(p) for p in posts} | {"blog.html"}
    for path in folder.glob("blog-*.html"):
        try:
            if MARK in path.read_text(encoding="utf-8") and path.name not in keep:
                path.unlink()
        except OSError:
            pass
    ensure_css(folder)
    cards = []
    for p in posts:
        pic = (f'<a href="{post_file(p)}" class="blog-card-pic"><img src="{html.escape(p["cover"], quote=True)}" alt="" loading="lazy"></a>'
               if p["cover"] else "")
        cards.append(f'    <li class="blog-card">{pic}<div><h2><a href="{post_file(p)}">{html.escape(p["title"])}</a></h2>'
                     f'<p class="blog-meta"><time datetime="{p["date"]}">{_pretty(p["date"])}</time></p>'
                     f'<p>{html.escape(_excerpt(p))} <a href="{post_file(p)}">Read more</a></p></div></li>')
    listing = ('  <ul class="blog-list">\n' + "\n".join(cards) + "\n  </ul>" if cards else
               '  <p class="blog-empty">No posts yet. Check back soon.</p>')
    feed_link = '<link rel="alternate" type="application/rss+xml" title="Blog feed" href="feed.xml">' if _site_url(folder) else ""
    _write_page(folder, "blog.html", "Blog", f'<main id="main" class="blog-page">\n  <h1>Blog</h1>\n{listing}\n</main>', feed_link)
    for p in posts:
        cover = (f'  <img class="blog-cover" src="{html.escape(p["cover"], quote=True)}" alt="">\n' if p["cover"] else "")
        main = (f'<main id="main" class="blog-page">\n<article class="blog-post">\n  <p class="back-link"><a href="blog.html">&larr; All posts</a></p>\n'
                f'  <h1>{html.escape(p["title"])}</h1>\n  <p class="blog-meta"><time datetime="{p["date"]}">{_pretty(p["date"])}</time></p>\n'
                f'{cover}{_body(p)}\n</article>\n</main>')
        _write_page(folder, post_file(p), p["title"], main, feed_link)
    notes = [f"Blog rebuilt: {len(posts)} published post(s)."]
    base = _site_url(folder)
    feed = folder / "feed.xml"
    if base:
        items = []
        for p in posts[:30]:
            dt = datetime.fromisoformat(p["date"]).replace(tzinfo=timezone.utc)
            url = f'{base}/{post_file(p)}'
            items.append(f"<item><title>{html.escape(p['title'])}</title><link>{html.escape(url)}</link>"
                         f"<guid>{html.escape(url)}</guid><pubDate>{format_datetime(dt)}</pubDate>"
                         f"<description>{html.escape(_excerpt(p))}</description></item>")
        site = html.escape(site_editor._title_of((folder / "index.html").read_text(encoding="utf-8"), folder.name))
        site_editor._write(feed, f'<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel><title>{site} blog</title>'
                                 f'<link>{html.escape(base)}/blog.html</link><description>{site} blog</description>'
                                 + "".join(items) + "</channel></rss>\n")
    elif feed.is_file():
        feed.unlink()
    return notes


BLOG_CSS = f"""
{CSS_MARK}
.blog-page{{max-width:48rem;margin:0 auto;padding:3rem 5%}}
.blog-list{{list-style:none;margin:2rem 0 0;padding:0;display:grid;gap:2.5rem}}
.blog-card{{display:grid;gap:1rem;grid-template-columns:minmax(0,12rem) 1fr;align-items:start}}
.blog-card:not(:has(.blog-card-pic)){{grid-template-columns:1fr}}
.blog-card-pic img{{display:block;width:100%;aspect-ratio:4/3;object-fit:cover}}
.blog-card h2{{margin:0 0 .25rem;font-size:1.4rem}}
.blog-meta{{margin:0 0 .5rem;opacity:.75;font-size:.9rem}}
.blog-cover,.blog-figure img{{display:block;width:100%;height:auto}}
.blog-figure{{margin:1.5rem 0}}.blog-figure figcaption{{opacity:.75;font-size:.9rem;margin-top:.4rem}}
.blog-post p{{line-height:1.7}}
.blog-button{{display:inline-block;padding:.65rem 1.3rem;border-radius:999px;background:var(--color-button-bg,#222);color:var(--color-button-text,#fff);text-decoration:none;font-weight:600}}
@media (max-width:560px){{.blog-card{{grid-template-columns:1fr}}}}
"""


def ensure_css(folder) -> None:
    css = Path(folder) / "styles.css"
    if css.is_file():
        text = css.read_text(encoding="utf-8")
        if CSS_MARK not in text:
            site_editor._write(css, text.rstrip() + "\n" + BLOG_CSS)


def install(folder) -> list:
    """Add the Blog as its own page with a link in the menu (works on sites built earlier)."""
    folder = Path(folder).resolve()
    notes = []
    notes += rebuild(folder)
    added = site_editor._add_menu_item(folder, "blog.html", "Blog")
    changed = site_editor.sync_shared(folder)
    notes.append("Added a Blog link to the menu." if added else "The menu already has a Blog link, or no menu list was found.")
    if changed:
        notes.append(f"The menu was copied to {len(changed)} other page(s).")
    return notes


def overview(folder) -> dict:
    folder = Path(folder)
    posts = sorted(load(folder), key=lambda p: (p["date"], p["id"]), reverse=True)
    return {"posts": [{k: p[k] for k in ("id", "title", "date", "status", "slug")} for p in posts],
            "installed": (folder / "blog.html").is_file(), "site_url_set": bool(_site_url(folder))}


def get_post(folder, pid) -> dict:
    post = next((p for p in load(folder) if p["id"] == pid), None)
    if post is None:
        raise BlogError("That post was not found.")
    return post
