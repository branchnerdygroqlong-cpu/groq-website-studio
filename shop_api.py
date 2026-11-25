"""Server-side shop API: the part of the cart that must NOT run in a browser.

It is used by the Studio (for previews) and by shop_server.py (to run a shop). A page can
only call these four routes:

    GET  api/capabilities   which plugins are set up     -> {"stripe": bool, "usps": bool}
    POST api/shipping       {cart, zip}                  -> {"options": [{service, label, cents}]}
    POST api/checkout       {cart, zip, shipping_service} -> {"url": "https://checkout.stripe.com/..."}
    POST api/track          {number}                     -> {"summary": "..."}

Rules this module enforces for you:
  * Prices and weights come from items.csv on the server. Prices sent by a browser are ignored.
  * Item ids, quantities and ZIP codes are validated; the cart is capped in size.
  * Shipping is re-priced on the server, so a shopper cannot choose their own price.
  * Error messages returned to browsers never contain API keys.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import quote

import plugin_stripe
import plugin_usps
import secrets_env
import shop_cart
import site_secrets

MAX_LINES, MAX_QTY = 50, 99
_hits = {}


def capabilities() -> dict:
    return {"stripe": plugin_stripe.configured(), "usps": plugin_usps.configured()}


def _limited(route: str, limit: int = 30) -> bool:
    """Crude protection for paid APIs: at most `limit` calls per route per minute."""
    now = time.time()
    recent = [t for t in _hits.get(route, []) if now - t < 60]
    recent.append(now)
    _hits[route] = recent
    return len(recent) > limit


def _cart_lines(folder: Path, cart):
    if not isinstance(cart, dict) or not cart:
        raise ValueError("Your cart is empty.")
    if len(cart) > MAX_LINES:
        raise ValueError("Too many different items in the cart.")
    items, _warnings = shop_cart.load_items(folder)
    catalog = {i["id"]: i for i in items}
    lines = []
    for item_id, qty in cart.items():
        item = catalog.get(str(item_id))
        if item is None:
            raise ValueError("An item in your cart is no longer for sale. Please review your cart.")
        if not item["in_stock"]:
            raise ValueError(f"{item['name']} is sold out.")
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            raise ValueError("Invalid quantity.")
        if not 1 <= qty <= MAX_QTY:
            raise ValueError(f"Quantity must be between 1 and {MAX_QTY}.")
        lines.append({"id": item["id"], "name": item["name"], "qty": qty,
                      "unit_cents": item["price_cents"], "weight_oz": item["weight_oz"]})
    return lines


def _shipping_for(lines, zip_code, service):
    weight = sum(l["weight_oz"] * l["qty"] for l in lines)
    for option in plugin_usps.rates(zip_code, weight):
        if option["service"] == service:
            return option
    raise ValueError("That shipping choice is not available. Please get shipping prices again.")


def handle(folder, route: str, body: dict, base_url: str):
    """Answers one shop request using this site's own keys (falling back to the shared .env)."""
    with site_secrets.use(folder):
        return _handle(folder, route, body, base_url)


def _handle(folder, route: str, body: dict, base_url: str):
    """Returns (http_status, json_dict). `base_url` is the public address of the site folder
    with a trailing slash (used to build Stripe's return links)."""
    folder = Path(folder)
    route = route.strip("/")
    try:
        if route == "capabilities":
            return 200, capabilities()
        if route in ("subscribe", "confirm", "unsubscribe"):
            import newsletter
            body = body if isinstance(body, dict) else {}
            return newsletter.public(folder, route, body)
        if _limited(route):
            return 429, {"error": "Too many requests. Please wait a minute."}
        body = body if isinstance(body, dict) else {}
        if route == "shipping":
            if not plugin_usps.configured():
                return 400, {"error": "USPS shipping is not set up on this site."}
            lines = _cart_lines(folder, body.get("cart"))
            weight = sum(l["weight_oz"] * l["qty"] for l in lines)
            return 200, {"options": plugin_usps.rates(str(body.get("zip", "")), weight)}
        if route == "checkout":
            if not plugin_stripe.configured():
                return 400, {"error": "Online payment is not set up on this site."}
            lines = _cart_lines(folder, body.get("cart"))
            shipping_cents, label = None, "Shipping"
            if plugin_usps.configured():
                service = str(body.get("shipping_service", ""))
                if not service:
                    return 400, {"error": "Choose a shipping option first."}
                option = _shipping_for(lines, str(body.get("zip", "")), service)
                shipping_cents, label = option["cents"], f"{option['label']} (USPS)"
            base = (secrets_env.get("SHOP_PUBLIC_URL") or base_url).rstrip("/") + "/"
            collect = secrets_env.get("SHOP_COLLECT_SHIPPING", "yes").lower() not in ("no", "false", "0")
            url = plugin_stripe.create_checkout_session(
                lines, currency=shop_cart.get_currency(folder),
                success_url=base + "cart.html?paid=1&session_id={CHECKOUT_SESSION_ID}",
                cancel_url=base + "cart.html", shipping_cents=shipping_cents,
                shipping_label=label, collect_address=collect)
            return 200, {"url": url}
        if route == "track":
            if not plugin_usps.configured():
                return 400, {"error": "USPS tracking is not set up on this site."}
            return 200, plugin_usps.track(str(body.get("number", "")))
        return 404, {"error": "unknown route"}
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except (plugin_stripe.StripeError, plugin_usps.UspsError) as exc:
        return 502, {"error": str(exc)}
    except Exception as exc:                         # never leak internals or keys to a browser
        print(f"[shop_api] {route} failed: {secrets_env.redact(repr(exc))}")
        return 500, {"error": "Something went wrong. Please try again."}


def public_base(scheme: str, host: str, prefix: str = "") -> str:
    host = re.sub(r"[^A-Za-z0-9.:\-\[\]]", "", host or "localhost")
    return f"{scheme}://{host}{quote(prefix)}/"
