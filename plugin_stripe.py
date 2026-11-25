#!/usr/bin/env python3
"""Stripe plugin: sends the shopper to Stripe's hosted checkout page.

Why this is safe for a static site
  * Your secret key lives only in .env on the server (see secrets_env.py). It is never
    written into any HTML or JavaScript file and the browser never sees it.
  * Stripe's own page takes the card details, so card numbers never touch this code.
  * Prices are looked up again from items.csv on the server; whatever prices a browser
    sends are ignored.

Set up (once):   python plugin_stripe.py setup      (or paste the key in the Studio's
                 "Payments and shipping" box)
Use a TEST key (starts with sk_test_ or rk_test_) first; real money only moves with a live key.
Not included: order emails, inventory counts and webhooks. Your Stripe Dashboard lists every payment.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import secrets_env

KEY_NAME = "STRIPE_SECRET_KEY"
API_URL = "https://api.stripe.com/v1/checkout/sessions"
MIN_CHARGE_CENTS = 50          # Stripe's minimum for USD; other currencies differ slightly


class StripeError(RuntimeError):
    """A problem that is safe to show to the shopper (never contains the key)."""


def configured() -> bool:
    return secrets_env.is_set(KEY_NAME)


def save_key(value: str) -> str:
    value = (value or "").strip()
    if not value.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_")):
        raise ValueError("That does not look like a Stripe secret key. It should start with "
                         "sk_test_ or sk_live_ (or rk_ for a restricted key). Do not use the "
                         "publishable key (pk_...).")
    secrets_env.set_secret(KEY_NAME, value)
    return secrets_env.mask(KEY_NAME)


def mode() -> str:
    key = secrets_env.get(KEY_NAME)
    return "live" if "_live_" in key else "test" if "_test_" in key else ""


def build_form(lines, *, currency, success_url, cancel_url, shipping_cents=None,
               shipping_label="Shipping", collect_address=True):
    """Return the form fields for the Checkout Session request (a list of key/value pairs)."""
    if not lines:
        raise StripeError("Your cart is empty.")
    form = [("mode", "payment"), ("success_url", success_url), ("cancel_url", cancel_url)]
    for i, line in enumerate(lines):
        base = f"line_items[{i}]"
        form += [
            (f"{base}[quantity]", str(int(line["qty"]))),
            (f"{base}[price_data][currency]", currency),
            (f"{base}[price_data][unit_amount]", str(int(line["unit_cents"]))),
            (f"{base}[price_data][product_data][name]", str(line["name"])[:250]),
        ]
    if collect_address:
        form.append(("shipping_address_collection[allowed_countries][0]", "US"))
    if shipping_cents is not None:
        base = "shipping_options[0][shipping_rate_data]"
        form += [
            (f"{base}[type]", "fixed_amount"),
            (f"{base}[display_name]", shipping_label[:100]),
            (f"{base}[fixed_amount][amount]", str(int(shipping_cents))),
            (f"{base}[fixed_amount][currency]", currency),
        ]
    return form


def _post(form):
    """The only place the secret key is used. Replaced by a fake in the tests."""
    key = secrets_env.get(KEY_NAME)
    if not key:
        raise StripeError("Stripe is not set up on this site.")
    request = urllib.request.Request(
        API_URL, data=urllib.parse.urlencode(form).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except (ValueError, OSError):
            message = ""
        print(f"[stripe] request refused ({exc.code}): {secrets_env.redact(message)}", file=sys.stderr)
        raise StripeError("Stripe could not start the checkout. The store owner should check "
                          "the Stripe key and the item prices.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise StripeError("Could not reach Stripe. Please try again in a moment.") from None


def create_checkout_session(lines, *, currency, success_url, cancel_url, shipping_cents=None,
                            shipping_label="Shipping", collect_address=True):
    """lines: [{"name", "unit_cents", "qty"}]. Returns the Stripe-hosted checkout URL."""
    total = sum(int(l["unit_cents"]) * int(l["qty"]) for l in lines) + int(shipping_cents or 0)
    if total < MIN_CHARGE_CENTS:
        raise StripeError("The total is below the minimum Stripe can charge.")
    session = _post(build_form(
        lines, currency=currency, success_url=success_url, cancel_url=cancel_url,
        shipping_cents=shipping_cents, shipping_label=shipping_label, collect_address=collect_address))
    url = session.get("url") if isinstance(session, dict) else None
    if not isinstance(url, str) or not url.startswith("https://"):
        raise StripeError("Stripe did not return a checkout link.")
    return url


def setup():
    import getpass
    print("Paste your Stripe SECRET key (input is hidden). Start with a test key (sk_test_...).")
    masked = save_key(getpass.getpass("Stripe secret key: "))
    print(f"Saved to {secrets_env.ENV_PATH} as {KEY_NAME} ({masked}, {mode()} mode).")


if __name__ == "__main__":
    if sys.argv[1:] == ["setup"]:
        setup()
    elif sys.argv[1:] == ["status"]:
        print(f"Stripe: {'set (' + mode() + ' mode, ' + secrets_env.mask(KEY_NAME) + ')' if configured() else 'not set'}")
    else:
        print(__doc__)
