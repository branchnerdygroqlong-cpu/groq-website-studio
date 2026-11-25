#!/usr/bin/env python3
"""USPS plugin: live shipping prices for the cart, and package tracking.

Uses the current USPS APIs (https://developers.usps.com). The old "Web Tools" service and
its User ID were shut down on January 25, 2026, so a Web Tools User ID will not work.
Create an app on the USPS developer portal to get a Consumer Key and Consumer Secret
(called Client ID and Client Secret here); the app must have the Prices and Tracking
products. Keep the secret private: it is stored only in .env on the server.

Set up (once):   python plugin_usps.py setup      (or use the Studio's
                 "Payments and shipping" box)
Settings stored in .env:
    USPS_CLIENT_ID, USPS_CLIENT_SECRET   your app credentials (secret)
    USPS_ORIGIN_ZIP                      the ZIP code you ship from
    USPS_API_BASE                        optional; https://apis-tem.usps.com is USPS's test server
    USPS_PACKAGE_INCHES                  optional box size, default 12x9x4
Prices are USPS retail prices for one standard box per order, using each item's weight_oz
from items.csv plus 4 oz of packaging. They are estimates for the shopper; Stripe charges
exactly the amount shown at checkout.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from decimal import Decimal, InvalidOperation

import secrets_env

ID_NAME, SECRET_NAME, ZIP_NAME = "USPS_CLIENT_ID", "USPS_CLIENT_SECRET", "USPS_ORIGIN_ZIP"
SERVICES = (("USPS_GROUND_ADVANTAGE", "USPS Ground Advantage"), ("PRIORITY_MAIL", "Priority Mail"))
PACKAGING_OZ = 4
CACHE_SECONDS = 600
_token = {"value": "", "expires": 0.0}
_quotes = {}


class UspsError(RuntimeError):
    """A problem that is safe to show to the shopper (never contains a secret)."""


def base_url() -> str:
    return (secrets_env.get("USPS_API_BASE") or "https://apis.usps.com").rstrip("/")


def configured() -> bool:
    return all(secrets_env.is_set(n) for n in (ID_NAME, SECRET_NAME, ZIP_NAME))


def save_credentials(client_id: str, client_secret: str, origin_zip: str) -> None:
    zip5 = re.sub(r"\D", "", origin_zip or "")[:5]
    if len(zip5) != 5:
        raise ValueError("The ZIP code you ship from must be 5 digits.")
    secrets_env.set_secret(ID_NAME, client_id)
    secrets_env.set_secret(SECRET_NAME, client_secret)
    secrets_env.set_secret(ZIP_NAME, zip5)
    _token.update(value="", expires=0.0)


def _package_inches():
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)\s*",
                         secrets_env.get("USPS_PACKAGE_INCHES") or "12x9x4")
    return tuple(float(v) for v in match.groups()) if match else (12.0, 9.0, 4.0)


def _http(method, path, body=None, token=None):
    """The only place USPS credentials are used. Replaced by a fake in the tests."""
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base_url() + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")[:300]
        except OSError:
            detail = ""
        print(f"[usps] {method} {path} refused ({exc.code}): {secrets_env.redact(detail)}", file=sys.stderr)
        if exc.code in (401, 403):
            raise UspsError("USPS did not accept the store's credentials.") from None
        if exc.code == 429:
            raise UspsError("USPS is busy. Please try again in a minute.") from None
        raise UspsError("USPS could not price this shipment.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise UspsError("Could not reach USPS. Please try again in a moment.") from None


def _get_token(force=False) -> str:
    if not force and _token["value"] and time.time() < _token["expires"] - 60:
        return _token["value"]
    data = _http("POST", "/oauth2/v3/token", {
        "client_id": secrets_env.get(ID_NAME), "client_secret": secrets_env.get(SECRET_NAME),
        "grant_type": "client_credentials"})
    value = data.get("access_token")
    if not value:
        raise UspsError("USPS did not return an access token.")
    try:
        lifetime = float(data.get("expires_in") or 1800)
    except (TypeError, ValueError):
        lifetime = 1800.0
    _token.update(value=value, expires=time.time() + lifetime)
    return value


def _call(method, path, body=None):
    try:
        return _http(method, path, body, _get_token())
    except UspsError as exc:
        if "credentials" not in str(exc):
            raise
        return _http(method, path, body, _get_token(force=True))       # token may have expired


def _cents(value):
    try:
        return int((Decimal(str(value)) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return None


def extract_price_cents(data):
    """USPS answers differ slightly between price products; look in the usual places."""
    if not isinstance(data, dict):
        return None
    for key in ("totalBasePrice", "totalPrice", "price"):
        if data.get(key) not in (None, ""):
            cents = _cents(data[key])
            if cents is not None and cents > 0:
                return cents
    rates = data.get("rates")
    if isinstance(rates, list):
        prices = [_cents(r.get("price")) for r in rates if isinstance(r, dict) and r.get("price") not in (None, "")]
        prices = [p for p in prices if p is not None]
        if prices and sum(prices) > 0:
            return sum(prices)
    return None


def rates(dest_zip: str, weight_oz: float):
    """Shipping choices for one package to dest_zip: [{service, label, cents}], cheapest first."""
    zip5 = re.sub(r"\D", "", dest_zip or "")[:5]
    if len(zip5) != 5:
        raise UspsError("Enter a 5-digit ZIP code.")
    origin = secrets_env.get(ZIP_NAME)
    if not origin:
        raise UspsError("USPS shipping is not set up on this site.")
    total_oz = max(1.0, float(weight_oz) + PACKAGING_OZ)
    pounds = round(total_oz / 16.0, 4)
    cache_key = (origin, zip5, pounds)
    hit = _quotes.get(cache_key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return [dict(o) for o in hit[1]]
    length, width, height = _package_inches()
    options, failures = [], 0
    for service, label in SERVICES:
        body = {"originZIPCode": origin, "destinationZIPCode": zip5, "weight": pounds,
                "length": length, "width": width, "height": height, "mailClass": service,
                "processingCategory": "MACHINABLE", "rateIndicator": "SP",
                "destinationEntryFacilityType": "NONE", "priceType": "RETAIL",
                "mailingDate": date.today().isoformat()}
        try:
            cents = extract_price_cents(_call("POST", "/prices/v3/base-rates/search", body))
        except UspsError as exc:
            if "credentials" in str(exc):
                raise                       # wrong key: do not burn quota trying the next service
            failures += 1
            continue
        if cents:
            options.append({"service": service, "label": label, "cents": cents})
    if not options:
        raise UspsError("USPS could not price this shipment." if failures else
                        "USPS returned no prices for that ZIP code.")
    options.sort(key=lambda o: o["cents"])
    _quotes[cache_key] = (time.time(), options)
    return [dict(o) for o in options]


def track(number: str):
    """Look up a tracking number. Returns {"summary": text}."""
    number = (number or "").strip().replace(" ", "")
    if not re.fullmatch(r"[A-Za-z0-9]{8,40}", number):
        raise UspsError("Enter a valid tracking number.")
    data = _call("GET", f"/tracking/v3/tracking/{urllib.parse.quote(number)}?expand=DETAIL")
    summary = ""
    if isinstance(data, dict):
        summary = str(data.get("statusSummary") or data.get("status") or data.get("statusCategory") or "")
    return {"summary": summary[:400] or "USPS has no details for that tracking number yet."}


def setup():
    import getpass
    print("USPS API credentials from https://developers.usps.com (your app's Consumer Key / Secret).")
    client_id = input("Client ID (Consumer Key): ").strip()
    client_secret = getpass.getpass("Client secret (input is hidden): ")
    origin = input("ZIP code you ship from: ").strip()
    save_credentials(client_id, client_secret, origin)
    print(f"Saved to {secrets_env.ENV_PATH} (secret {secrets_env.mask(SECRET_NAME)}).")


if __name__ == "__main__":
    if sys.argv[1:] == ["setup"]:
        setup()
    elif sys.argv[1:] == ["status"]:
        print(f"USPS: {'set, shipping from ' + secrets_env.get(ZIP_NAME) if configured() else 'not set'}")
    else:
        print(__doc__)
