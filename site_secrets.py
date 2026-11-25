#!/usr/bin/env python3
"""Keep each website's payment and shipping keys in that website's own folder.

    projects/<site>/.shop_secrets.env      (hidden file, owner-only permissions)

* The file is plain KEY=value lines, the same names the Studio's shared .env uses.
* Visitors can never download it: the Studio and shop_server.py refuse every hidden file, and the
  keys are never written into a web page. The browser form only ever sees the last 4 characters.
* While the shop API answers a request for a site, `use(folder)` makes that site's keys the ones
  `secrets_env.get()` returns. A site with no key of its own falls back to the Studio's shared .env,
  so nothing that works today stops working.
* Works on sites built earlier: it only needs the folder, not anything added when the site was built.
Standard library only.
"""
from __future__ import annotations

import contextlib
import os
import re
import tempfile
import threading
from pathlib import Path

import secrets_env

FILE = ".shop_secrets.env"
ALLOWED = ("STRIPE_SECRET_KEY", "USPS_CLIENT_ID", "USPS_CLIENT_SECRET", "USPS_ORIGIN_ZIP",
           "SHOP_PUBLIC_URL", "SHOP_COLLECT_SHIPPING", "NEWS_SMTP_PASSWORD", "CLOUDFLARE_API_TOKEN")


def _names():
    """Use the real variable names from the plugins when they exist, so the two always agree."""
    names = list(ALLOWED)
    try:
        import plugin_stripe
        import plugin_usps
        names[0], names[1], names[2], names[3] = (plugin_stripe.KEY_NAME, plugin_usps.ID_NAME,
                                                   plugin_usps.SECRET_NAME, plugin_usps.ZIP_NAME)
    except Exception:
        pass
    return tuple(names)


_local = threading.local()
_orig_get, _orig_mask, _orig_redact = secrets_env.get, getattr(secrets_env, "mask", None), getattr(secrets_env, "redact", None)


def _path(folder) -> Path:
    return Path(folder) / FILE


def read(folder) -> dict:
    try:
        text = _path(folder).read_text(encoding="utf-8")
    except OSError:
        return {}
    values, allowed = {}, set(_names())
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key in allowed and value:
            values[key] = value
    return values


def write(folder, values: dict) -> None:
    allowed = set(_names())
    lines = ["# Payment and shipping keys for this website only. Keep this file private:",
             "# do not email it, upload it to a public host, or put it in a public repository."]
    for key in _names():
        value = values.get(key)
        if value and key in allowed:
            if re.search(r"[\r\n\x00]", value):
                raise ValueError("Keys cannot contain line breaks.")
            lines.append(f"{key}={value}")
    path = _path(folder)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _get(name, default=""):
    folder = getattr(_local, "folder", None)
    if folder is not None:
        value = read(folder).get(name)
        if value:
            return value
    return _orig_get(name, default) if _takes_default() else (_orig_get(name) or default)


def _takes_default():
    try:
        import inspect
        return len(inspect.signature(_orig_get).parameters) >= 2
    except (TypeError, ValueError):
        return False


def _mask(name, *args, **kwargs):
    folder = getattr(_local, "folder", None)
    value = read(folder).get(name) if folder is not None else None
    if value:
        return ("*" * 4 + value[-4:]) if len(value) > 8 else "*" * 8
    return _orig_mask(name, *args, **kwargs)


def _redact(text, *args, **kwargs):
    text = _orig_redact(text, *args, **kwargs) if _orig_redact else text
    folder = getattr(_local, "folder", None)
    if folder is not None and isinstance(text, str):
        for key, value in read(folder).items():
            if key not in ("SHOP_PUBLIC_URL", "SHOP_COLLECT_SHIPPING") and len(value) >= 6:
                text = text.replace(value, "[hidden]")
    return text


secrets_env.get = _get
if _orig_mask:
    secrets_env.mask = _mask
if _orig_redact:
    secrets_env.redact = _redact


@contextlib.contextmanager
def use(folder):
    """Inside this block (this thread only) the site's own keys win over the shared ones."""
    previous = getattr(_local, "folder", None)
    _local.folder = Path(folder)
    try:
        yield
    finally:
        _local.folder = previous


# ---- what the form may do ------------------------------------------------------------------
STRIPE_RE = re.compile(r"^(sk|rk)_(test|live)_[A-Za-z0-9]{10,200}$")
ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")
TOKEN_RE = re.compile(r"^[\x21-\x7e]{8,200}$")


def save(folder, stripe_key="", usps_id="", usps_secret="", usps_zip=""):
    """Add or change keys. Blank boxes keep what is already saved. Returns what was saved."""
    stripe, uid, usec, uzip = _names()[:4]
    values, saved = read(folder), []
    if stripe_key:
        if not STRIPE_RE.match(stripe_key):
            raise ValueError("That does not look like a Stripe secret key. It starts with sk_test_ or sk_live_ "
                             "(a restricted key starting rk_ also works). Do not use the publishable pk_ key.")
        values[stripe] = stripe_key
        saved.append("Stripe key")
    if usps_id or usps_secret or usps_zip:
        for label, v in (("Client ID", usps_id), ("Client secret", usps_secret)):
            if v and not TOKEN_RE.match(v):
                raise ValueError(f"The USPS {label} should be 8 to 200 characters with no spaces.")
        if usps_zip and not ZIP_RE.match(usps_zip):
            raise ValueError("Enter the ZIP code you ship from, like 60601.")
        for key, v in ((uid, usps_id), (usec, usps_secret), (uzip, usps_zip)):
            if v:
                values[key] = v
        if not (values.get(uid) and values.get(usec) and values.get(uzip)):
            raise ValueError("USPS needs the Client ID, the Client secret and the ZIP code you ship from.")
        saved.append("USPS details")
    if not saved:
        raise ValueError("Nothing to save: paste a key first.")
    write(folder, values)
    return saved


def clear(folder, which: str):
    stripe, uid, usec, uzip = _names()[:4]
    values = read(folder)
    drop = {"stripe": [stripe], "usps": [uid, usec, uzip], "all": list(_names())}.get(which)
    if drop is None:
        raise ValueError("Unknown choice.")
    for key in drop:
        values.pop(key, None)
    if values:
        write(folder, values)
    else:
        _path(folder).unlink(missing_ok=True)


def import_shared(folder, which="all"):
    """Copy the keys saved in the Studio's shared .env into this site (for sites made earlier)."""
    stripe, uid, usec, uzip = _names()[:4]
    wanted = {"stripe": [stripe], "usps": [uid, usec, uzip]}.get(which, list(_names()))
    values, copied = read(folder), []
    for key in wanted:
        if values.get(key):
            continue
        shared = _orig_get(key) if not _takes_default() else _orig_get(key, "")
        if shared:
            values[key] = shared
            copied.append(key)
    if not copied:
        raise ValueError("There are no shared keys to copy, or this site already has them all.")
    write(folder, values)
    return copied


def status(folder) -> dict:
    """Safe to show in the page: only whether keys exist, their last 4 characters, and where they come from."""
    import plugin_stripe
    import plugin_usps
    folder = Path(folder)
    own = read(folder)
    stripe, uid, usec, uzip = _names()[:4]
    with use(folder):
        stripe_ok, usps_ok = bool(plugin_stripe.configured()), bool(plugin_usps.configured())
        try:
            mode = plugin_stripe.mode()
        except Exception:
            mode = ""
        masked = {k: secrets_env.mask(k) for k in (stripe, usec, uid)}
        zip_code = secrets_env.get(uzip)
    def source(keys):
        if all(own.get(k) for k in keys):
            return "site"
        return "shared" if any(_orig_get(k) for k in keys) else "none"
    return {"stripe": {"set": stripe_ok, "masked": masked[stripe], "mode": mode, "source": source([stripe]),
                       "has_own": bool(own.get(stripe))},
            "usps": {"set": usps_ok, "masked": masked[usec], "client_id": masked[uid], "origin_zip": zip_code,
                     "source": source([uid, usec, uzip]), "has_own": bool(own.get(usec))},
            "has_shop": (folder / "cart.html").is_file() and (folder / "cart.js").is_file(),
            "file": FILE}
