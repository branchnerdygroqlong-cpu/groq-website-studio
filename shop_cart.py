#!/usr/bin/env python3
"""Standalone shopping cart for a generated Groq Website Studio site.

    python shop_cart.py projects/my-site            add or refresh the shop
    python shop_cart.py projects/my-site --remove   take the shop back out

What it does
  * reads  <site>/items.csv  (your item and price list; created on first run)
  * writes shop.html, cart.html, cart.js and shop.css into the site folder
  * adds "Shop" and "Cart (n)" links to the navigation of every page, plus the
    stylesheet and script tags those pages need
  * is safe to run again: change a price or add a row in items.csv, run it again

items.csv columns (only id, name and price are required; section is optional and groups items under a heading):
    id,name,price,description,image,weight_oz,in_stock,section
    tee-black,Black T-Shirt,24.00,Soft cotton tee.,assets/tee.jpg,8,yes
Lines that start with # are ignored. Excel or Numbers can open and save the file
(choose "CSV"). Prices are plain numbers; $ signs are fine.

The cart itself runs in the visitor's browser (it remembers items in that browser only).
Paying and shipping prices need the optional Stripe and USPS plugins (plugin_stripe.py,
plugin_usps.py), which run on the server. See shop_api.py. This file has no dependencies
beyond the Python standard library and does not import any other studio file.
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

ITEMS_FILE = "items.csv"
SETTINGS_FILE = "shop.json"
MAX_ITEMS = 200
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
TRUTHY = {"", "y", "yes", "true", "1", "in stock", "instock", "available"}

ITEMS_TEMPLATE = """\
# Your item and price list. Edit this file, then run:  python shop_cart.py <this site's folder>
# (or press "Update contact / shop" for the site in the Studio). Lines starting with # are ignored.
#
# Columns:   id = short lowercase code (letters, digits, - or _), must be unique
#            name, price (a number like 24.00), description (optional)
#            image = a file inside this site's folder, e.g. assets/tee.jpg (optional)
#            weight_oz = shipping weight in ounces (optional; used for USPS prices)
#            in_stock = yes or no (optional; blank means yes)
#
# Example row (remove the leading # to use it):
# tee-black,Black T-Shirt,24.00,Soft cotton tee.,assets/tee.jpg,8,yes
id,name,price,description,image,weight_oz,in_stock,section
"""

CURRENCY_SYMBOLS = {"usd": "$", "eur": "\u20ac", "gbp": "\u00a3", "cad": "CA$", "aud": "A$"}


# --------------------------------------------------------------------------- #
# The item list
# --------------------------------------------------------------------------- #
def _parse_price(raw: str):
    text = str(raw or "").strip().replace("$", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", text):
        text = text.replace(",", "")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", text):
        raise ValueError(f"price {raw!r} is not a number like 24.00")
    try:
        cents = int((Decimal(text) * 100).to_integral_value())
    except InvalidOperation:
        raise ValueError(f"price {raw!r} is not a number like 24.00")
    if cents > 100_000_000:
        raise ValueError(f"price {raw!r} is too large")
    return cents


def load_items(folder, create=False):
    """Return (items, warnings). Each item is a dict with id, name, price_cents,
    description, image, weight_oz, in_stock. Bad rows are skipped with a warning."""
    folder = Path(folder)
    path = folder / ITEMS_FILE
    warnings = []
    if not path.is_file():
        if create:
            path.write_text(ITEMS_TEMPLATE, encoding="utf-8", newline="\n")
            warnings.append(f"created {ITEMS_FILE} with no items yet; open it and add rows")
        return [], warnings
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return [], warnings
    delimiter = ";" if lines[0].count(";") > lines[0].count(",") else ","
    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    reader.fieldnames = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    missing = {"id", "name", "price"} - set(reader.fieldnames)
    if missing:
        warnings.append(f"{ITEMS_FILE} needs the columns id, name, price (missing: {', '.join(sorted(missing))})")
        return [], warnings
    items, seen = [], set()
    root = folder.resolve()
    for number, row in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k}
        label = row.get("id") or row.get("name") or f"row {number}"
        try:
            item_id = row.get("id", "").lower()
            if not ID_RE.match(item_id):
                raise ValueError("id must be lowercase letters, digits, - or _ (for example tee-black)")
            if item_id in seen:
                raise ValueError("duplicate id")
            if not row.get("name"):
                raise ValueError("name is empty")
            cents = _parse_price(row.get("price"))
            weight = 8.0
            if row.get("weight_oz"):
                try:
                    weight = float(row["weight_oz"])
                except ValueError:
                    raise ValueError(f"weight_oz {row['weight_oz']!r} is not a number")
                if not 0 < weight <= 1120:
                    raise ValueError("weight_oz must be between 0 and 1120")
            image = row.get("image", "").replace("\\", "/").lstrip("/")
            if image:
                target = (root / image).resolve()
                if root not in target.parents or not target.is_file():
                    warnings.append(f"{label}: image {image!r} not found in the site folder; shown without a picture")
                    image = ""
        except ValueError as exc:
            warnings.append(f"{ITEMS_FILE} row {number} ({label}) skipped: {exc}")
            continue
        seen.add(item_id)
        items.append({
            "id": item_id, "name": row["name"][:120], "price_cents": cents,
            "description": row.get("description", "")[:400], "image": image,
            "weight_oz": weight,
            "in_stock": row.get("in_stock", "").strip().lower() in TRUTHY,
            "section": " ".join(row.get("section", "").split())[:60],
        })
        if len(items) >= MAX_ITEMS:
            warnings.append(f"only the first {MAX_ITEMS} items are used")
            break
    return items, warnings


def _slug_id(name: str, used: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:30].strip("-") or "item"
    candidate, n = base, 2
    while candidate in used:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


OTHER_SECTION = "More"


def group_items(items):
    """[(section name, [items])] in the order each section first appears. Items with no section are
    shown under "More" when other sections exist, and with no heading when none do."""
    order, groups = [], {}
    for item in items:
        name = item.get("section", "")
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(item)
    named = [n for n in order if n]
    if not named:
        return [("", items)] if items else []
    result = [(n, groups[n]) for n in named]
    if "" in groups:
        result.append((OTHER_SECTION, groups[""]))
    return result


def section_slug(name: str) -> str:
    return "section-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "x")


PAGE_MARK = "<!-- studio-shop-page -->"


def section_file(name: str) -> str:
    return "shop-" + section_slug(name)[len("section-"):] + ".html"


def item_href(item, items) -> str:
    """The shop page that shows this item in full (its section's page, or shop.html)."""
    if any(i.get("section") for i in items):
        return f"{section_file(item.get('section') or OTHER_SECTION)}#item-{item['id']}"
    return f"shop.html#item-{item['id']}"


def _price_text(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def items_for_form(folder):
    """The item list as plain rows for an editing form: (rows, warnings)."""
    items, warnings = load_items(folder)
    rows = [{"id": i["id"], "name": i["name"], "price": _price_text(i["price_cents"]),
             "description": i["description"], "image": i["image"],
             "weight_oz": "" if i["weight_oz"] == 8.0 else f"{i['weight_oz']:g}",
             "in_stock": i["in_stock"], "section": i.get("section", "")} for i in items]
    return rows, warnings


def clean_rows(folder, rows):
    """Check rows typed into a form. Returns (items, errors) where errors maps a row number
    (as text) to a message the shop owner can act on. Nothing is written here."""
    root = Path(folder).resolve()
    items, errors, used = [], {}, set()
    if not isinstance(rows, list):
        return [], {"0": "The item list was not understood."}
    if len(rows) > MAX_ITEMS:
        return [], {"0": f"A shop can have at most {MAX_ITEMS} items."}
    for number, row in enumerate(rows):
        try:
            row = row if isinstance(row, dict) else {}
            name = " ".join(str(row.get("name", "")).split())
            if not name:
                raise ValueError("Enter an item name.")
            try:
                cents = _parse_price(row.get("price"))
            except ValueError:
                raise ValueError("Enter the price as a number such as 24.00.")
            weight = 8.0
            if str(row.get("weight_oz", "")).strip():
                try:
                    weight = float(str(row["weight_oz"]).strip())
                except ValueError:
                    raise ValueError("Weight must be a number of ounces, or left empty.")
                if not 0 < weight <= 1120:
                    raise ValueError("Weight must be between 0 and 1120 ounces.")
            image = str(row.get("image", "")).replace("\\", "/").lstrip("/")
            if image:
                target = (root / image).resolve()
                if root not in target.parents or not target.is_file():
                    image = ""
            wanted = str(row.get("id", "")).strip().lower()
            item_id = wanted if ID_RE.match(wanted) and wanted not in used else _slug_id(name, used)
            used.add(item_id)
            items.append({"id": item_id, "name": name[:120], "price_cents": cents,
                          "description": " ".join(str(row.get("description", "")).split())[:400],
                          "image": image, "weight_oz": weight, "in_stock": bool(row.get("in_stock", True)),
                          "section": " ".join(str(row.get("section", "")).split())[:60]})
        except ValueError as exc:
            errors[str(number)] = str(exc)
    return items, errors


def save_items(folder, items) -> None:
    """Write validated items (from clean_rows) to items.csv."""
    out = io.StringIO()
    out.write("# Your item and price list. The Studio's Edit shop items form writes this file;\n"
              "# you can also edit it by hand. Lines starting with # are ignored.\n")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["id", "name", "price", "description", "image", "weight_oz", "in_stock", "section"])
    for i in items:
        writer.writerow([i["id"], i["name"], _price_text(i["price_cents"]), i["description"], i["image"],
                         f"{i['weight_oz']:g}", "yes" if i["in_stock"] else "no", i.get("section", "")])
    _write(Path(folder) / ITEMS_FILE, out.getvalue())


def get_currency(folder) -> str:
    try:
        value = json.loads((Path(folder) / SETTINGS_FILE).read_text(encoding="utf-8")).get("currency", "usd")
    except (OSError, ValueError):
        value = "usd"
    value = str(value).lower()
    return value if re.fullmatch(r"[a-z]{3}", value) else "usd"


def money(cents: int, currency: str) -> str:
    symbol = CURRENCY_SYMBOLS.get(currency.lower())
    amount = f"{cents // 100:,}.{cents % 100:02d}"
    return f"{symbol}{amount}" if symbol else f"{currency.upper()} {amount}"


# --------------------------------------------------------------------------- #
# HTML helpers
# --------------------------------------------------------------------------- #
def _write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    os.replace(tmp, path)


def _extract(page: str, tag: str) -> str:
    match = re.search(rf"<{tag}\b.*?</{tag}>", page, re.S | re.I)
    return match.group(0) if match else ""


def _strip_shop(page: str) -> str:
    """Remove everything this file added to a page, so it can be added again cleanly."""
    page = re.sub(r"\s*<li\b[^>]*\bdata-shop-link\b[^>]*>.*?</li>", "", page, flags=re.S | re.I)
    page = re.sub(r"\s*<a\b[^>]*\bdata-shop-link\b[^>]*>.*?</a>", "", page, flags=re.S | re.I)
    page = re.sub(r"\s*<nav\b[^>]*\bdata-shop-nav\b[^>]*>.*?</nav>", "", page, flags=re.S | re.I)
    page = re.sub(r"\s*<link\b[^>]*\bdata-shop\b[^>]*>", "", page, flags=re.I)
    page = re.sub(r"\s*<script\b[^>]*\bdata-shop\b[^>]*>\s*</script>", "", page, flags=re.I)
    return page


def _retarget_shop_links(page: str) -> str:
    """Point the site's own "Shop" links (for example #shop-plants-merch) at shop.html, so the
    navigation does not end up with two Shop links."""
    pattern = (r"""(<a\b[^>]*?\shref\s*=\s*)(["'])(?:index\.html)?#[^"']*shop[^"']*\2"""
               r"""([^>]*>\s*Shop\s*</a>)""")
    return re.sub(pattern, lambda m: f'{m.group(1)}"shop.html"{m.group(3)}', page, flags=re.I)


SYNC_START, SYNC_END = "<!-- shop-sync:start -->", "<!-- shop-sync:end -->"
SYNC_BACKUP = "index.html.before_shopsync"


TEASER_MAX = 110


def _teaser(item, href=None) -> str:
    """One sentence of the description followed by a "..." link to the full text on the shop page."""
    text = " ".join(str(item.get("description", "")).split())
    if not text:
        return ""
    first = re.match(r"(.+?[.!?])(?:\s|$)", text)
    short = first.group(1) if first else text
    cut = len(short) < len(text)
    if len(short) > TEASER_MAX:
        short = short[:TEASER_MAX].rsplit(" ", 1)[0].rstrip(" ,;:-")
        cut = True
    more = (f' <a class="product-more" href="{html.escape(href or 'shop.html#item-' + item['id'], quote=True)}" '
            f'aria-label="Read the full description of {html.escape(item["name"], quote=True)}">&hellip;</a>') if cut else ""
    return f'<p class="product-care">{html.escape(short)}{more}</p>'


def _sync_block(items, currency) -> str:
    data = {"currency": currency, "products": [
        {"id": i["id"], "name": i["name"], "price_cents": i["price_cents"],
         "image": i["image"], "in_stock": i["in_stock"]} for i in items]}
    named = any(n for n, _ in group_items(items))
    lvl = 4 if named else 3

    def card(item):
        img = (f'<img src="{html.escape(item["image"], quote=True)}" alt="{html.escape(item["name"], quote=True)}" '
               f'class="product-image" loading="lazy">') if item["image"] else ""
        desc = _teaser(item, item_href(item, items))
        button = (f'<button type="button" class="add-to-cart-link shop-add" data-id="{item["id"]}">Add to Cart</button>'
                  if item["in_stock"] else
                  '<button type="button" class="add-to-cart-link shop-add" disabled>Sold out</button>')
        return (f'    <article class="product-card">\n      {img}\n'
                f'      <h{lvl} class="product-name"><a href="{html.escape(item_href(item, items), quote=True)}">{html.escape(item["name"])}</a></h{lvl}>\n      {desc}\n'
                f'      <p class="product-price">{html.escape(money(item["price_cents"], currency))}</p>\n'
                f'      {button}\n    </article>')
    groups = group_items(items)
    sections = []
    for name, group in groups:
        head = (f'  <h3 class="shop-section-title"><a href="{section_file(name)}">{html.escape(name)}</a></h3>\n'
                if name else "")
        sections.append(f'  <div class="shop-section">\n{head}  <div class="products-grid">\n'
                        + "\n".join(card(i) for i in group) + "\n  </div>\n  </div>")
    grid = "\n".join(sections) if sections else '  <p class="shop-empty">New plants are coming soon.</p>'
    return (f'{SYNC_START}\n<div class="shop-sync" data-shop-sync>\n'
            f'  <p id="shop-status" class="shop-status" role="status" aria-live="polite"></p>\n{grid}\n'
            f'  <p class="shop-viewcart"><a href="shop.html">See the whole shop</a> &middot; '
            f'<a href="cart.html">View your cart</a></p>\n'
            f'  <script type="application/json" id="shop-products">{_json_for_script(data)}</script>\n'
            f'</div>\n{SYNC_END}')


def _sync_homepage(folder: Path, items, currency) -> str:
    """Make the homepage's own shop section show the real items from items.csv."""
    path = folder / "index.html"
    page = path.read_text(encoding="utf-8")
    block = _sync_block(items, currency)
    if SYNC_START in page and SYNC_END in page:
        new = re.sub(re.escape(SYNC_START) + r".*?" + re.escape(SYNC_END), lambda m: block, page, flags=re.S)
    else:
        sec = re.search(r"""(<section\b[^>]*\bid\s*=\s*["'][^"']*shop[^"']*["'][^>]*>)(.*?)(</section>)""",
                        page, re.S | re.I)
        if not sec:
            return "[shop] No shop section found on the homepage; left it alone."
        heading = re.search(r"<h2\b.*?</h2>", sec.group(2), re.S | re.I)
        head = heading.group(0) if heading else ""
        backup = folder / SYNC_BACKUP
        if not backup.exists():
            backup.write_text(page, encoding="utf-8", newline="\n")
        new = (page[:sec.start()] + sec.group(1) + "\n  " + head + "\n" + block + "\n" + sec.group(3)
               + page[sec.end():])
    if new != page:
        _write(path, new)
    return f"[shop] Homepage shop section now shows {len(items)} item(s) from {ITEMS_FILE}."


def _inject_nav(page: str) -> str:
    page = _retarget_shop_links(page)
    has_shop = re.search(r"""href\s*=\s*["']shop\.html["']""", page, re.I)
    has_cart = re.search(r"""href\s*=\s*["']cart\.html["']""", page, re.I)
    shop_a = '<a href="shop.html" data-shop-link>Shop</a>'
    cart_a = ('<a href="cart.html" data-shop-link class="cart-link">Cart '
              '<span class="cart-count" data-cart-count hidden>0</span></a>')
    if has_shop and has_cart:
        return page
    parts = ([] if has_shop else [shop_a]) + ([] if has_cart else [cart_a])
    nav = re.search(r"<nav\b[^>]*>.*?</nav>", page, re.S | re.I)
    if nav:
        block = nav.group(0)
        if re.search(r"</ul>", block, re.I):
            idx = block.lower().rfind("</ul>")
            new = block[:idx].rstrip() + "".join(f"<li data-shop-link>{p}</li>" for p in parts) + block[idx:]
        else:
            idx = block.lower().rfind("</nav>")
            new = block[:idx].rstrip() + "".join(parts) + block[idx:]
        return page[:nav.start()] + new + page[nav.end():]
    extra = '<nav class="shop-nav" data-shop-nav aria-label="Shop">' + "".join(parts) + "</nav>"
    if re.search(r"</header>", page, re.I):
        idx = page.lower().rfind("</header>")
        return page[:idx].rstrip() + extra + page[idx:]
    return re.sub(r"(<body\b[^>]*>)", lambda m: m.group(1) + "\n" + extra, page, count=1, flags=re.I)


def _inject_assets(page: str) -> str:
    link = '<link rel="stylesheet" href="shop.css" data-shop>'
    script = '<script src="cart.js" defer data-shop></script>'
    match = re.search(r"</head>", page, re.I)
    if match:
        page = page[:match.start()].rstrip() + "\n" + link + "\n" + page[match.start():]
    if re.search(r"</body>", page, re.I):
        idx = page.lower().rfind("</body>")
        page = page[:idx].rstrip() + "\n" + script + "\n" + page[idx:]
    return page


def _ids(page: str) -> set:
    return set(re.findall(r"""\sid\s*=\s*["']([^"']+)["']""", page))


def _for_subpage(fragment: str, index_ids: set) -> str:
    """Make a copy of the homepage header/footer work from another page."""
    def fix(match):
        name = match.group(2)
        return f'{match.group(1)}"index.html#{name}"' if name in index_ids else match.group(0)
    fragment = re.sub(r"""(<a\b[^>]*?\shref\s*=\s*)["']#([A-Za-z][^"']*)["']""", fix, fragment, flags=re.I)
    first = re.search(r"""<a\b[^>]*?\shref\s*=\s*(["'])(.*?)\1""", fragment, re.S | re.I)
    if first and first.group(2).strip() in ("", "#") and re.match(r"\s*<header", fragment, re.I):
        # the first link in a header is the logo / site name: send it to the main page
        fragment = fragment[:first.start(2)] + "index.html" + fragment[first.end(2):]
    return fragment


def _site_name(index: str, folder: Path) -> str:
    title = re.search(r"<title>(.*?)</title>", index, re.S | re.I)
    name = html.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip() if title else ""
    return (name.split("|")[-1].strip() if "|" in name else name) or folder.name.replace("-", " ").title()


def _json_for_script(data) -> str:
    return (json.dumps(data, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _document(site, title, header, main, footer):
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} | {html.escape(site)}</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
{PAGE_MARK}
{header}

{main}

{footer}
</body>
</html>
"""


def _products_json(items, currency):
    data = {"currency": currency, "products": [
        {"id": i["id"], "name": i["name"], "price_cents": i["price_cents"],
         "image": i["image"], "in_stock": i["in_stock"]} for i in items]}
    return f'<script type="application/json" id="shop-products">{_json_for_script(data)}</script>'


def _item_card(item, currency, lvl):
    img = (f'<img class="shop-item-image" src="{html.escape(item["image"], quote=True)}" '
           f'alt="{html.escape(item["name"], quote=True)}" loading="lazy">') if item["image"] else ""
    desc = f'<p class="shop-item-desc">{html.escape(item["description"])}</p>' if item["description"] else ""
    button = (f'<button type="button" class="shop-add" data-id="{item["id"]}">Add to cart</button>'
              if item["in_stock"] else '<button type="button" class="shop-add" disabled>Sold out</button>')
    return (f'    <li class="shop-item" id="item-{item["id"]}">\n      {img}\n'
            f'      <h{lvl} class="shop-item-name">{html.escape(item["name"])}</h{lvl}>\n      {desc}\n'
            f'      <p class="shop-item-price">{html.escape(money(item["price_cents"], currency))}</p>\n'
            f'      {button}\n    </li>')


def _shop_page(items, currency, title, group, back, intro_html):
    grid = ('  <ul class="shop-grid">\n' + "\n".join(_item_card(i, currency, 2) for i in group) + "\n  </ul>"
            if group else
            f'  <p class="shop-empty">No items are for sale yet. The store owner can add them in the '
            f'Studio (Edit shop items) or in {ITEMS_FILE}.</p>')
    wrap = f'  <div class="shop-sections-wrap">\n{grid}\n  </div>' if group else grid
    return f"""<main id="main" class="shop-page">
  <p class="back-link">{back}</p>
  <h1>{html.escape(title)}</h1>
{intro_html}  <p id="shop-status" class="shop-status" role="status" aria-live="polite"></p>
{wrap}
  <p class="shop-viewcart"><a href="cart.html">View your cart</a></p>
  <noscript><p>The cart needs JavaScript. Please turn it on to add items.</p></noscript>
  {_products_json(items, currency)}
</main>"""


def _shop_pages(items, currency):
    """{file name: (page title, main html)}. With no sections that is just shop.html with every item.
    With sections, shop.html lists the sections and each section gets a page of its own."""
    groups = group_items(items)
    if not any(n for n, _ in groups):
        return {"shop.html": ("Shop", _shop_page(items, currency, "Shop", items,
                                                 '<a href="index.html">&larr; Back to the main page</a>', ""))}
    cards = []
    for name, group in groups:
        pic = next((i["image"] for i in group if i["image"]), "")
        img = (f'<img class="shop-section-image" src="{html.escape(pic, quote=True)}" alt="" loading="lazy">'
               if pic else "")
        count = f'{len(group)} item{"s" if len(group) != 1 else ""}'
        cards.append(f'    <li class="shop-section-card"><a href="{section_file(name)}">{img}'
                     f'<h2 class="shop-section-title">{html.escape(name)}</h2>'
                     f'<span class="shop-section-count">{count}</span></a></li>')
    landing = f"""<main id="main" class="shop-page">
  <p class="back-link"><a href="index.html">&larr; Back to the main page</a></p>
  <h1>Shop</h1>
  <ul class="shop-section-list">
{chr(10).join(cards)}
  </ul>
  <p class="shop-viewcart"><a href="cart.html">View your cart</a></p>
  {_products_json(items, currency)}
</main>"""
    pages = {"shop.html": ("Shop", landing)}
    for name, group in groups:
        pages[section_file(name)] = (name, _shop_page(
            items, currency, name, group, '<a href="shop.html">&larr; All shop sections</a>', ""))
    return pages


def _cart_main(items, currency):
    data = {"currency": currency, "products": [
        {"id": i["id"], "name": i["name"], "price_cents": i["price_cents"],
         "image": i["image"], "in_stock": i["in_stock"]} for i in items]}
    return f"""<main id="main" class="shop-page">
  <p class="back-link"><a href="index.html">&larr; Back to the main page</a></p>
  <h1>Your cart</h1>
  <p id="cart-status" class="shop-status" role="status" aria-live="polite"></p>
  <div id="cart-root"><noscript><p>The cart needs JavaScript. Please turn it on to see your cart.</p></noscript></div>
  <p class="shop-viewcart"><a href="shop.html">Continue shopping</a></p>
  <script type="application/json" id="shop-products">{_json_for_script(data)}</script>
</main>"""


# --------------------------------------------------------------------------- #
# Browser files
# --------------------------------------------------------------------------- #
CART_JS = r"""/* Shopping cart for this site. Generated by shop_cart.py; safe to regenerate.
   The cart only remembers item ids and quantities in this browser. Prices always come
   from the product list embedded in the page, and the server re-checks them at payment. */
(function () {
  'use strict';
  var KEY = 'studio_cart_v1';
  var ID = /^[a-z0-9][a-z0-9_-]{0,40}$/;
  var products = {}, currency = 'usd';
  var holder = document.getElementById('shop-products');
  if (holder) {
    try {
      var parsed = JSON.parse(holder.textContent);
      currency = parsed.currency || 'usd';
      (parsed.products || []).forEach(function (p) { products[p.id] = p; });
    } catch (e) { /* no product data on this page */ }
  }
  var INFO_KEY = 'studio_cart_info_v1', paneRefresh = function () {};
  /* Names and prices are remembered (for display only) so the cart pane works on every page. */
  function readInfo() {
    try { var v = JSON.parse(localStorage.getItem(INFO_KEY) || '{}'); return v && typeof v === 'object' ? v : {}; }
    catch (e) { return {}; }
  }
  (function () {
    var ids = Object.keys(products);
    if (!ids.length) { return; }
    var info = readInfo();
    ids.forEach(function (id) {
      info[id] = { name: String(products[id].name || id), price_cents: products[id].price_cents | 0 };
    });
    try { localStorage.setItem(INFO_KEY, JSON.stringify(info)); } catch (e) { /* storage blocked */ }
  })();

  function readCart() {
    var cart = {};
    try {
      var raw = JSON.parse(localStorage.getItem(KEY) || '{}');
      Object.keys(raw || {}).forEach(function (id) {
        var qty = parseInt(raw[id], 10);
        if (ID.test(id) && qty > 0) { cart[id] = Math.min(qty, 99); }
      });
    } catch (e) { /* ignore a damaged cart */ }
    return cart;
  }
  function writeCart(cart) {
    try { localStorage.setItem(KEY, JSON.stringify(cart)); } catch (e) { /* storage blocked */ }
    badges(cart);
  }
  function count(cart) {
    return Object.keys(cart).reduce(function (n, id) { return n + cart[id]; }, 0);
  }
  function badges(cart) {
    var n = count(cart || readCart());
    Array.prototype.forEach.call(document.querySelectorAll('[data-cart-count]'), function (el) {
      el.textContent = String(n);
      el.hidden = n === 0;
    });
    paneRefresh();
  }
  function fmt(cents) {
    try {
      return new Intl.NumberFormat(undefined, { style: 'currency', currency: currency.toUpperCase() }).format(cents / 100);
    } catch (e) { return (cents / 100).toFixed(2) + ' ' + currency.toUpperCase(); }
  }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }
  function say(id, text) {
    var node = document.getElementById(id);
    if (node) { node.textContent = text || ''; }
  }

  /* ---------------- shop page ---------------- */
  var shop = document.querySelector('.shop-sections-wrap, .shop-grid, [data-shop-sync]');
  if (shop) {
    shop.addEventListener('click', function (event) {
      var button = event.target.closest('.shop-add[data-id]');
      if (!button || button.disabled) { return; }
      var id = button.getAttribute('data-id');
      if (!products[id]) { return; }
      var cart = readCart();
      cart[id] = Math.min((cart[id] || 0) + 1, 99);
      writeCart(cart);
      if (readCart()[id] !== cart[id]) {
        say('shop-status', 'This browser would not save your cart. If you opened the file by double-clicking it, ' +
          'open the site through the Studio or shop_server.py instead.');
        return;
      }
      say('shop-status', products[id].name + ' added to your cart.');
    });
  }

  /* ---------------- cart page ---------------- */
  var root = document.getElementById('cart-root');
  if (root) {
    var caps = { stripe: false, usps: false };
    var ship = { zip: '', options: [], chosen: '' };
    var busy = false;

    if (/[?&]paid=1\b/.test(location.search)) {
      writeCart({});
      say('cart-status', 'Thank you for your order. Your payment went through.');
    }

    fetch('api/capabilities', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (c) { caps.stripe = !!c.stripe; caps.usps = !!c.usps; render(); })
      .catch(function () { render(); });

    var render = function () {
      var cart = readCart(), ids = Object.keys(cart).filter(function (id) { return products[id]; });
      var pruned = {};
      ids.forEach(function (id) { pruned[id] = cart[id]; });
      if (ids.length !== Object.keys(cart).length) { writeCart(pruned); cart = pruned; }
      root.textContent = '';
      if (!ids.length) {
        root.appendChild(el('p', 'shop-empty', 'Your cart is empty.'));
        return;
      }
      var list = el('ul', 'cart-lines'), subtotal = 0;
      ids.forEach(function (id) {
        var p = products[id], qty = cart[id], line = p.price_cents * qty;
        subtotal += line;
        var li = el('li', 'cart-line');
        var pic = el('span', 'cart-pic');
        if (p.image) {
          var thumb = el('img', 'cart-thumb'); thumb.src = p.image; thumb.alt = '';
          pic.appendChild(thumb);
        }
        li.appendChild(pic);
        li.appendChild(el('span', 'cart-name', p.name));
        li.appendChild(el('span', 'cart-unit', fmt(p.price_cents)));
        var q = el('span', 'cart-qty');
        var minus = el('button', 'cart-step', '\u2212'); minus.type = 'button';
        minus.setAttribute('aria-label', 'One less ' + p.name);
        var num = el('span', 'cart-num', String(qty));
        var plus = el('button', 'cart-step', '+'); plus.type = 'button';
        plus.setAttribute('aria-label', 'One more ' + p.name);
        q.appendChild(minus); q.appendChild(num); q.appendChild(plus);
        li.appendChild(q);
        li.appendChild(el('span', 'cart-line-total', fmt(line)));
        var remove = el('button', 'cart-remove', 'Remove'); remove.type = 'button';
        remove.setAttribute('aria-label', 'Remove ' + p.name + ' from the cart');
        li.appendChild(remove);
        function change(delta) {
          var c = readCart(), next = (c[id] || 0) + delta;
          if (next <= 0) { delete c[id]; } else { c[id] = Math.min(next, 99); }
          writeCart(c); ship.options = []; ship.chosen = ''; say('cart-status', ''); render();
        }
        minus.onclick = function () { change(-1); };
        plus.onclick = function () { change(1); };
        remove.onclick = function () { change(-99); };
        list.appendChild(li);
      });
      root.appendChild(list);

      var chosen = ship.options.filter(function (o) { return o.service === ship.chosen; })[0];
      var totals = el('dl', 'cart-totals');
      function row(label, value) { totals.appendChild(el('dt', '', label)); totals.appendChild(el('dd', '', value)); }
      row('Subtotal', fmt(subtotal));
      if (chosen) { row('Shipping (' + chosen.label + ')', fmt(chosen.cents)); }
      row('Total', fmt(subtotal + (chosen ? chosen.cents : 0)));
      root.appendChild(totals);

      if (caps.usps) {
        var form = el('div', 'cart-ship');
        var label = el('label', '', 'ZIP code for shipping prices'); label.setAttribute('for', 'cart-zip');
        var zip = el('input'); zip.id = 'cart-zip'; zip.type = 'text'; zip.inputMode = 'numeric';
        zip.maxLength = 10; zip.autocomplete = 'postal-code'; zip.value = ship.zip;
        var go = el('button', 'cart-button', 'Get shipping prices'); go.type = 'button';
        go.onclick = function () {
          var z = zip.value.trim();
          if (!/^\d{5}(-\d{4})?$/.test(z)) { say('cart-status', 'Enter a 5-digit ZIP code.'); return; }
          ship.zip = z; go.disabled = true; say('cart-status', 'Getting USPS prices...');
          post('api/shipping', { cart: readCart(), zip: z }).then(function (d) {
            ship.options = d.options || []; ship.chosen = ship.options.length ? ship.options[0].service : '';
            say('cart-status', ship.options.length ? '' : 'USPS returned no prices for that ZIP code.'); render();
          }).catch(function (e) { go.disabled = false; say('cart-status', e.message); });
        };
        form.appendChild(label); form.appendChild(zip); form.appendChild(go);
        if (ship.options.length) {
          var group = el('div', 'cart-ship-options');
          ship.options.forEach(function (o) {
            var lab = el('label', 'cart-ship-option');
            var radio = el('input'); radio.type = 'radio'; radio.name = 'ship'; radio.value = o.service;
            radio.checked = o.service === ship.chosen;
            radio.onchange = function () { ship.chosen = o.service; render(); };
            lab.appendChild(radio);
            lab.appendChild(document.createTextNode(' ' + o.label + ' \u2013 ' + fmt(o.cents)));
            group.appendChild(lab);
          });
          form.appendChild(group);
        }
        root.appendChild(form);
      }

      var pay = el('button', 'cart-button cart-pay', caps.stripe ? 'Checkout with Stripe' : 'Checkout');
      pay.type = 'button';
      pay.disabled = !caps.stripe || busy || (caps.usps && !chosen);
      if (caps.stripe) {
        pay.onclick = function () {
          busy = true; pay.disabled = true; say('cart-status', 'Opening secure checkout...');
          post('api/checkout', { cart: readCart(), zip: ship.zip, shipping_service: ship.chosen }).then(function (d) {
            if (d.url) { location.href = d.url; } else { throw new Error('No checkout link was returned.'); }
          }).catch(function (e) { busy = false; say('cart-status', e.message); render(); });
        };
      }
      root.appendChild(pay);
      if (!caps.stripe) {
        root.appendChild(el('p', 'shop-hint', 'Online payment is not set up on this site yet.'));
      } else if (caps.usps && !chosen) {
        root.appendChild(el('p', 'shop-hint', 'Get shipping prices to continue.'));
      }
    };

    function post(url, body) {
      return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
        .then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (d) {
            if (!r.ok) { throw new Error(d.error || 'Something went wrong. Please try again.'); }
            return d;
          });
        });
    }
    render();
  }

  /* Floating cart button and pane: always in the top corner while the page is scrolled. */
  function floatingCart() {
    if (document.querySelector('[data-cart-fab]') || /(^|\/)cart\.html$/.test(location.pathname)) { return; }
    var NS = 'http://www.w3.org/2000/svg';
    var wrap = el('div', 'cart-fab-wrap');
    wrap.setAttribute('data-cart-fab', '');
    var btn = el('button', 'cart-fab');
    btn.type = 'button';
    btn.setAttribute('aria-label', 'Shopping cart');
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-controls', 'cart-pane');
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('width', '24'); svg.setAttribute('height', '24');
    svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2'); svg.setAttribute('stroke-linecap', 'round'); svg.setAttribute('stroke-linejoin', 'round');
    var path = document.createElementNS(NS, 'path');
    path.setAttribute('d', 'M3 4h2.5l2.2 10.2a1 1 0 0 0 1 .8h8.6a1 1 0 0 0 1-.8L20 8H6.2');
    svg.appendChild(path);
    [[9.5, 19.5], [17, 19.5]].forEach(function (c) {
      var dot = document.createElementNS(NS, 'circle');
      dot.setAttribute('cx', c[0]); dot.setAttribute('cy', c[1]); dot.setAttribute('r', '1.3'); svg.appendChild(dot);
    });
    var badge = el('span', 'cart-count', '0');
    badge.setAttribute('data-cart-count', ''); badge.hidden = true;
    btn.appendChild(svg); btn.appendChild(badge);

    var pane = el('div', 'cart-pane');
    pane.id = 'cart-pane'; pane.hidden = true;
    pane.setAttribute('role', 'region'); pane.setAttribute('aria-label', 'Your shopping cart');
    wrap.appendChild(btn); wrap.appendChild(pane);
    document.body.appendChild(wrap);

    function draw() {
      var cart = readCart(), ids = Object.keys(cart), info = readInfo(), total = 0;
      pane.textContent = '';
      pane.appendChild(el('h2', 'cart-pane-title', 'Your cart'));
      if (!ids.length) { pane.appendChild(el('p', 'cart-pane-empty', 'Your cart is empty.')); return; }
      var list = el('ul', 'cart-pane-lines');
      ids.forEach(function (id) {
        var known = products[id] || info[id] || null, price = known ? (known.price_cents | 0) : 0;
        var li = el('li', 'cart-pane-line');
        li.appendChild(el('span', 'cart-pane-name', (known && known.name ? known.name : id) + ' × ' + cart[id]));
        if (known) { li.appendChild(el('span', 'cart-pane-price', fmt(price * cart[id]))); total += price * cart[id]; }
        list.appendChild(li);
      });
      pane.appendChild(list);
      pane.appendChild(el('p', 'cart-pane-total', 'Subtotal: ' + fmt(total)));
      var go = el('a', 'cart-pane-go', 'View cart and check out');
      go.href = 'cart.html';
      pane.appendChild(go);
    }
    function open(state) {
      pane.hidden = !state;
      btn.setAttribute('aria-expanded', state ? 'true' : 'false');
      if (state) { draw(); }
    }
    btn.addEventListener('click', function () { open(pane.hidden); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !pane.hidden) { open(false); btn.focus(); } });
    document.addEventListener('click', function (e) { if (!pane.hidden && !wrap.contains(e.target)) { open(false); } });
    paneRefresh = function () { if (!pane.hidden) { draw(); } };
    window.addEventListener('storage', paneRefresh);
  }

  floatingCart();
  badges();
  window.addEventListener('storage', function () { badges(); });
})();
"""

SHOP_CSS = """/* Shop and cart styles. They borrow the site's own colours and fonts; edit freely. */
.shop-page{max-width:68rem;margin:0 auto;padding:3rem 5% 5rem}
.back-link{margin:0 0 1.5rem}
.shop-status{min-height:1.5em;margin:.5rem 0 0}
.shop-section-list{list-style:none;margin:2rem 0 0;padding:0;display:grid;gap:1.75rem;grid-template-columns:repeat(auto-fill,minmax(15rem,1fr))}
.shop-section-card a{display:flex;flex-direction:column;gap:.5rem;color:inherit;text-decoration:none}
.shop-section-card .shop-section-title{margin:0;font-size:1.3rem}
.shop-section-image{display:block;width:100%;aspect-ratio:4/3;object-fit:cover}
.shop-sections{display:flex;flex-wrap:wrap;gap:.5rem 1.25rem;margin:1.5rem 0 0}
.shop-section-title{margin:2.5rem 0 0}
.shop-section:first-child .shop-section-title{margin-top:1.5rem}
.shop-grid{list-style:none;margin:2rem 0 0;padding:0;display:grid;gap:2.5rem 1.75rem;grid-template-columns:repeat(auto-fill,minmax(15rem,1fr))}
.shop-item{display:flex;flex-direction:column;gap:.5rem}
.shop-item-image{display:block;width:100%;aspect-ratio:4/3;object-fit:cover}
.shop-item-name{margin:.5rem 0 0;font-size:1.2rem}
.shop-item-desc{margin:0;opacity:.8}
.shop-item-price{margin:0;font-weight:700}
.shop-add,.cart-button,.cart-step,.cart-remove{font:inherit;color:inherit;background:transparent;border:2px solid currentColor;border-radius:0;padding:.6rem 1.1rem;cursor:pointer}
.shop-add{align-self:flex-start}
.shop-add:hover,.cart-button:hover,.cart-step:hover,.cart-remove:hover{background:rgba(128,128,128,.2)}
.shop-add:focus-visible,.cart-button:focus-visible,.cart-step:focus-visible,.cart-remove:focus-visible,.cart-ship input:focus-visible{outline:3px solid currentColor;outline-offset:3px}
.shop-add:disabled,.cart-button:disabled{opacity:.5;cursor:not-allowed}
.shop-viewcart{margin:2.5rem 0 0}
.shop-empty,.shop-hint{margin:1.5rem 0 0;opacity:.85}
.cart-lines{list-style:none;margin:2rem 0 0;padding:0}
.cart-line{display:grid;grid-template-columns:4rem minmax(8rem,1fr) auto auto auto auto;gap:.75rem 1.25rem;align-items:center;padding:1rem 0;border-top:1px solid rgba(128,128,128,.4)}
.cart-line:last-child{border-bottom:1px solid rgba(128,128,128,.4)}
.cart-name{font-weight:700}
.cart-pic{display:block;width:4rem;min-height:1px}
.cart-thumb{display:block;width:4rem;height:4rem;object-fit:cover}
.cart-qty{display:inline-flex;align-items:center;gap:.5rem}
.cart-step{padding:.2rem .7rem}
.cart-remove{padding:.3rem .8rem;font-size:.9rem}
.cart-line-total{text-align:right;min-width:5rem}
.cart-totals{display:grid;grid-template-columns:1fr auto;gap:.4rem 2rem;max-width:26rem;margin:1.5rem 0 0 auto}
.cart-totals dt,.cart-totals dd{margin:0}
.cart-totals dd{text-align:right}
.cart-totals dt:last-of-type,.cart-totals dd:last-of-type{font-weight:700;font-size:1.15rem;padding-top:.5rem}
.cart-ship{margin:2rem 0 0;display:flex;flex-wrap:wrap;gap:.75rem;align-items:center}
.cart-ship label{font-weight:700}
.cart-ship input[type=text]{font:inherit;color:inherit;background:transparent;border:2px solid currentColor;padding:.55rem .7rem;width:9rem}
.cart-ship-options{flex-basis:100%;display:grid;gap:.4rem}
.cart-ship .cart-ship-option{font-weight:400}
.cart-pay{margin-top:2rem;font-weight:700}
.cart-link .cart-count,.cart-count{display:inline-block;min-width:1.5em;padding:0 .4em;margin-left:.35em;border:1px solid currentColor;border-radius:1em;font-size:.8em;line-height:1.5;text-align:center}
.cart-count[hidden]{display:none!important}
.product-name a{color:inherit;text-decoration:none}
.product-more{font-weight:700;text-decoration:none;padding:0 .25em}
.cart-fab-wrap{position:fixed;top:var(--cart-fab-top,1rem);right:var(--cart-fab-right,1rem);z-index:1000;display:flex;flex-direction:column;align-items:flex-end;gap:.5rem}
.cart-fab{display:inline-flex;align-items:center;gap:.25rem;padding:.65rem .8rem;border:0;border-radius:999px;cursor:pointer;font:inherit;background:var(--color-button-bg,#222);color:var(--color-button-text,#fff);box-shadow:0 2px 10px rgba(0,0,0,.3)}
.cart-fab:hover,.cart-fab:focus-visible{outline:3px solid currentColor;outline-offset:2px}
.cart-fab .cart-count{margin-left:0;border-color:currentColor}
.cart-pane{width:min(22rem,calc(100vw - 2rem));max-height:70vh;overflow:auto;padding:1rem;border-radius:.75rem;background:var(--color-white,#fff);color:var(--color-text,#222);border:1px solid rgba(0,0,0,.15);box-shadow:0 6px 24px rgba(0,0,0,.25);text-align:left}
.cart-pane[hidden]{display:none}
.cart-pane-title{margin:0 0 .5rem;font-size:1.1rem}
.cart-pane-lines{list-style:none;margin:0;padding:0}
.cart-pane-line{display:flex;justify-content:space-between;gap:1rem;padding:.35rem 0;border-bottom:1px solid rgba(0,0,0,.1)}
.cart-pane-total{margin:.6rem 0;font-weight:700}
.cart-pane-go{display:block;text-align:center;padding:.6rem 1rem;border-radius:999px;background:var(--color-button-bg,#222);color:var(--color-button-text,#fff);text-decoration:none;font-weight:600}
@media (max-width:40rem){.cart-line{grid-template-columns:4rem 1fr auto}.cart-unit,.cart-qty,.cart-line-total,.cart-remove{grid-column:auto}}
"""


# --------------------------------------------------------------------------- #
# Putting it together
# --------------------------------------------------------------------------- #
def _remove_shop_pages(folder: Path) -> None:
    for path in folder.glob("shop-*.html"):
        try:
            if PAGE_MARK in path.read_text(encoding="utf-8"):
                path.unlink()
        except OSError:
            pass


def build_shop(folder, currency=None):
    """Create or refresh the shop in a site folder. Returns a list of status lines."""
    folder = Path(folder).resolve()
    index_path = folder / "index.html"
    if not index_path.is_file():
        raise RuntimeError(f"{folder} has no index.html; build the website first.")
    currency = (currency or get_currency(folder)).lower()
    if not re.fullmatch(r"[a-z]{3}", currency):
        raise RuntimeError("currency must be a 3-letter code such as usd")
    notes = []
    items, warnings = load_items(folder, create=True)
    notes += [f"[shop] {w}" for w in warnings]

    _remove_shop_pages(folder)               # section pages are rebuilt below
    pages = sorted(p for p in folder.glob("*.html") if p.name not in ("shop.html", "cart.html"))
    for path in pages:                       # strip first so index is clean before copying chrome
        _write(path, _strip_shop(path.read_text(encoding="utf-8")))
    for path in pages:
        text = path.read_text(encoding="utf-8")
        _write(path, _inject_assets(_inject_nav(text)))

    notes.append(_sync_homepage(folder, items, currency))
    index = index_path.read_text(encoding="utf-8")
    site = _site_name(index, folder)
    index_ids = _ids(index)
    header = _for_subpage(_extract(index, "header"), index_ids)
    footer = _for_subpage(_extract(index, "footer"), index_ids)
    if not header:
        header = f'<header class="shop-header"><nav aria-label="Main"><a href="index.html">{html.escape(site)}</a></nav></header>'
    made = dict(_shop_pages(items, currency))
    made["cart.html"] = ("Your cart", _cart_main(items, currency))
    for name, (title, main) in made.items():
        _write(folder / name, _inject_assets(_document(site, title, header, main, footer)))

    _write(folder / "cart.js", CART_JS)
    _write(folder / "shop.css", SHOP_CSS)
    _write(folder / SETTINGS_FILE, json.dumps({"currency": currency}) + "\n")
    notes.append(f"[shop] {len(items)} item(s) from {ITEMS_FILE}; wrote {', '.join(sorted(made))}, cart.js, shop.css; "
                 f"Shop and Cart links added to {len(pages)} page(s).")
    if not items:
        notes.append(f"[shop] No items yet: open {folder / ITEMS_FILE} and add rows, then run this again.")
    return notes


def remove_shop(folder):
    folder = Path(folder).resolve()
    count = 0
    _remove_shop_pages(folder)
    for path in folder.glob("*.html"):
        if path.name in ("shop.html", "cart.html"):
            path.unlink()
            continue
        text = path.read_text(encoding="utf-8")
        new = _strip_shop(text)
        new = re.sub(r"\s*" + re.escape(SYNC_START) + r".*?" + re.escape(SYNC_END), "", new, flags=re.S)
        if new != text:
            _write(path, new)
            count += 1
    for name in ("cart.js", "shop.css", SETTINGS_FILE):
        (folder / name).unlink(missing_ok=True)
    return [f"[shop] Removed the shop from {count} page(s). {ITEMS_FILE} was kept."]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Add or refresh a shopping cart in a generated site.")
    parser.add_argument("folder", help="the site folder, for example projects/my-site")
    parser.add_argument("--currency", help="3-letter currency code (default usd)")
    parser.add_argument("--remove", action="store_true", help="remove the shop from the site")
    args = parser.parse_args(argv)
    try:
        lines = remove_shop(args.folder) if args.remove else build_shop(args.folder, args.currency)
    except (RuntimeError, OSError) as exc:
        sys.exit(f"shop_cart: {exc}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
