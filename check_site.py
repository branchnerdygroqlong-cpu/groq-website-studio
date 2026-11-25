#!/usr/bin/env python3
"""Audit a generated site and summarize its build log. Read-only: changes nothing.

Usage:
  python check_site.py projects/SITE_FOLDER
  python check_site.py projects/SITE_FOLDER --log logs/test1.log --brief "the prompt you used"
"""
import argparse
import re
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

EXTERNAL = ("http://", "https://", "mailto:", "tel:", "javascript:", "data:")
WANTS_FORM = re.compile(r"quote|form|contact|order|inquir|enquir|book|appointment|subscribe|"
                        r"newsletter|sign.?up|rsvp|apply|request", re.I)
SPECIFICS = (r"\b\d{1,2}:\d{2}\s?(?:am|pm|a\.m\.|p\.m\.)?", r"\$\s?\d[\d,.]*",
             r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b", r"\b(?:19|20)\d{2}\b")


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids, self.links, self.imgs, self.classes = [], [], [], set()
        self.text, self.h1, self.forms, self.stylesheets = [], 0, 0, []
        self.title, self._in_title, self._skip, self._a = "", False, 0, None

    def handle_starttag(self, tag, attrs):
        a = dict((k, v or "") for k, v in attrs)
        if a.get("id"):
            self.ids.append(a["id"])
        self.classes.update(a.get("class", "").split())
        if tag == "h1":
            self.h1 += 1
        elif tag == "form":
            self.forms += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("script", "style"):
            self._skip += 1
        elif tag == "link" and "stylesheet" in a.get("rel", ""):
            self.stylesheets.append(a.get("href", ""))
        elif tag == "img":
            self.imgs.append((a.get("src", ""), "alt" in a))
            if self._a is not None:
                self._a["label"] += " " + a.get("alt", "")
        elif tag == "a" and "href" in a:
            self._a = {"href": a["href"].strip(), "label": a.get("aria-label", "")}

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._a is not None:
            self.links.append(self._a)
            self._a = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.text.append(data)
            if self._a is not None:
                self._a["label"] += " " + data

    @property
    def visible(self):
        return " ".join(" ".join(self.text).split())


def audit(folder: Path, brief: str):
    errors, warnings, info = [], [], []
    pages = {}
    for path in sorted(folder.glob("*.html")):
        parser = Page()
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        pages[path.name] = parser
    if "index.html" not in pages:
        errors.append("no index.html")
        return errors, warnings, info
    info.append(f"pages: {', '.join(pages)}")

    css_path = folder / "styles.css"
    css = css_path.read_text(encoding="utf-8", errors="replace") if css_path.is_file() else ""
    if not css:
        errors.append("styles.css missing or empty")
    # extra stylesheets the studio adds (for example shop.css) count towards coverage
    css += "\n" + "\n".join(p.read_text(encoding="utf-8", errors="replace")
                             for p in sorted(folder.glob("*.css")) if p.name != "styles.css")
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    if css and plain.count("{") != plain.count("}"):
        errors.append("styles.css has unbalanced braces")

    placeholder_links = broken = missing_pages = missing_imgs = no_alt = 0
    dead_labels, broken_list = [], []
    placeholders = Counter()
    for name, page in pages.items():
        if page.h1 != 1:
            (errors if page.h1 == 0 else warnings).append(f"{name}: {page.h1} <h1> elements (want 1)")
        dupes = [i for i, n in Counter(page.ids).items() if n > 1]
        if dupes:
            warnings.append(f"{name}: duplicate ids {dupes[:4]}")
        for sheet in page.stylesheets:
            if not (folder / sheet).is_file():
                errors.append(f"{name}: stylesheet {sheet} not found")
        for link in page.links:
            href = link["href"]
            label = " ".join(link["label"].split())[:30]
            if href in ("", "#"):
                placeholder_links += 1
                dead_labels.append(f"{name}: '{label}'")
            elif href.startswith(EXTERNAL):
                continue
            elif href.startswith("#"):
                if href[1:] not in page.ids:
                    broken += 1
                    broken_list.append(f"{name}: {href}")
            else:
                target, _, frag = unquote(href).partition("#")
                target = target.split("?")[0]
                if not (folder / target).is_file():
                    missing_pages += 1
                    broken_list.append(f"{name}: {href} (file missing)")
                elif frag and target in pages and frag not in pages[target].ids:
                    broken += 1
                    broken_list.append(f"{name}: {href} (anchor missing)")
        for src, has_alt in page.imgs:
            if not src.startswith(EXTERNAL) and not (folder / src).is_file():
                missing_imgs += 1
                broken_list.append(f"{name}: image {src} missing")
            if not has_alt:
                no_alt += 1
        placeholders.update(re.findall(r"\[[A-Za-z][^\]]{1,40}\]", page.visible))

    if broken or missing_pages or missing_imgs:
        errors.append(f"broken anchors: {broken}, missing pages: {missing_pages}, "
                      f"missing images: {missing_imgs}")
        errors.extend("   " + b for b in broken_list[:8])
    if placeholder_links:
        warnings.append(f"{placeholder_links} placeholder links (href=\"#\"): "
                        + "; ".join(dead_labels[:6]) + (" ..." if len(dead_labels) > 6 else ""))
    if no_alt:
        warnings.append(f"{no_alt} images without an alt attribute")
    if placeholders:
        info.append(f"[bracket] placeholders: {sum(placeholders.values())} "
                    f"({', '.join(list(placeholders)[:6])})")

    # CSS coverage: do the classes the HTML uses actually have rules?
    used = set().union(*(p.classes for p in pages.values())) if pages else set()
    defined = set(re.findall(r"\.([A-Za-z_][\w-]*)", plain))
    if used:
        covered = len(used & defined)
        pct = 100 * covered / len(used)
        info.append(f"CSS: {len(plain)} bytes, ~{plain.count('{')} rules; "
                    f"{covered}/{len(used)} HTML classes styled ({pct:.0f}%)")
        if pct < 75:
            warnings.append("low CSS coverage; unstyled classes: "
                            + ", ".join(sorted(used - defined)[:10]))
    variables_used = set(re.findall(r"var\((--[\w-]+)", plain))
    variables_defined = set(re.findall(r"(--[\w-]+)\s*:", plain))
    if variables_used - variables_defined:
        warnings.append("CSS uses undefined variables: "
                        + ", ".join(sorted(variables_used - variables_defined)[:6]))

    # features the brief did not ask for
    all_text = re.sub(r"\[[^\]]*\]", " ", " ".join(p.visible for p in pages.values()))  # ignore [placeholders]
    forms = sum(p.forms for p in pages.values())
    if brief:
        if forms and not WANTS_FORM.search(brief):
            warnings.append(f"{forms} form(s) but the brief did not ask for any")
        if re.search(r"\bquote\b", all_text, re.I) and not re.search(r"quote", brief, re.I):
            warnings.append("'quote' wording appears but the brief did not ask for it")
        found = []
        items_file = folder / "items.csv"          # prices typed into the shop's item list are intended
        allowed = brief.lower() + "\n" + (items_file.read_text(encoding="utf-8", errors="replace").lower()
                                          if items_file.is_file() else "")
        for pattern in SPECIFICS:
            for m in re.finditer(pattern, all_text, re.I):
                token = m.group(0).strip().lower()
                if token not in allowed and token.lstrip("$ ").replace(",", "") not in allowed:
                    found.append(m.group(0).strip())
        if found:
            warnings.append("specifics not in the brief (times/prices/phones/years): "
                            + ", ".join(dict.fromkeys(found))[:100])
    elif forms:
        info.append(f"{forms} form(s) found (pass --brief to check whether they were requested)")

    credits = folder / "assets" / "CREDITS.txt"
    if credits.is_file():
        if not re.search(r"credit|photo by|image by|photograph|license|wikimedia", all_text, re.I):
            warnings.append("images are credited in assets/CREDITS.txt but the site shows no attribution")
    return errors, warnings, info


def summarize_log(path: Path):
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    totals, last, calls = [], 0, 0
    for line in lines:
        m = re.search(r"\(build total (\d+)/", line)
        if m:
            calls += 1
            value = int(m.group(1))
            if value < last:        # budget was reset: a new phase (e.g. page expansion)
                totals.append(last)
            last = value
    totals.append(last)
    waits = sum(1 for l in lines if re.search(r"waiting \d+s", l))
    handoffs = sum(1 for l in lines if re.search(r"\] .*: .* failed:", l))
    ok_models = Counter(re.findall(r"\bok \(([\w./-]+)", "\n".join(lines)))
    out = [
        f"model calls: {calls}; tokens used: {sum(totals)} "
        f"({' + '.join(map(str, totals))} per phase)",
        f"successful generations by model: {dict(ok_models) or 'none'}",
        f"repair requests: {sum('to repair' in l for l in lines)}",
        f"continuations after truncation: {sum('continuation' in l and 'truncated' in l for l in lines)}",
        f"waits (rate limit / quota): {waits}",
        f"model failures handed to next model: {handoffs}",
        "images: " + ", ".join(f"{k}={v}" for k, v in Counter(
            re.findall(r"\[images\]\s+(SUCCESS|UNAVAILABLE|ERROR)", "\n".join(lines))).items() or [("none", 0)]),
    ]
    if any("Traceback" in l or l.startswith("ERROR") for l in lines):
        out.append("BUILD ERROR present in log")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--log")
    ap.add_argument("--brief", default="")
    args = ap.parse_args()
    folder = Path(args.folder)
    if not folder.is_dir():
        sys.exit(f"Folder not found: {folder}")
    print(f"=== {folder} ===")
    errors, warnings, info = audit(folder, args.brief)
    for line in info:
        print("  info   ", line)
    for line in errors:
        print("  ERROR  ", line)
    for line in warnings:
        print("  warn   ", line)
    groups = len([e for e in errors if not e.startswith("   ")])
    print(f"  RESULT: {groups} error group(s), {len(warnings)} warning(s)")
    if args.log:
        print("\n=== build log ===")
        for line in summarize_log(Path(args.log)):
            print("  " + line)


if __name__ == "__main__":
    main()
