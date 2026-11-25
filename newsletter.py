#!/usr/bin/env python3
"""Newsletter for a generated site: sign-ups, a subscriber list, writing and sending by email.

Everything for one site lives in that site's folder:
    .newsletter/subscribers.db      subscribers, newsletters, and a log of who was sent what
    .newsletter/settings.json       sender name/address, postal address, mail server (no password)
    .shop_secrets.env               the mail account password (NEWS_SMTP_PASSWORD), same hidden file as the shop keys
Hidden folders are never served by the Studio or shop_server.py, so visitors cannot download any of it.

Mail goes out through SMTP, which every provider offers (Gmail with an app password, Brevo, Mailgun,
Amazon SES, Resend, SendGrid ...). Standard library only.

Rules it follows for you: sign-ups are confirmed by email first (double opt-in) unless you turn that off,
every newsletter has an unsubscribe link and your postal address, and unsubscribed people are never mailed.
"""
from __future__ import annotations

import contextlib
import csv
import html
import io
import json
import os
import re
import secrets
import smtplib
import socket
import sqlite3
import ssl
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

import site_secrets

DIR = ".newsletter"
PAGE_MARK = "<!-- studio-news-page -->"
BLOCK_START, BLOCK_END = "<!-- studio-news:start -->", "<!-- studio-news:end -->"
CSS_MARK = "/* STUDIO-NEWS */"
PASSWORD_KEY = "NEWS_SMTP_PASSWORD"
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
PRESETS = {
    "gmail": {"label": "Gmail (needs an app password)", "host": "smtp.gmail.com", "port": 465, "security": "ssl", "limit": 400},
    "brevo": {"label": "Brevo", "host": "smtp-relay.brevo.com", "port": 587, "security": "starttls", "limit": 250},
    "mailgun": {"label": "Mailgun", "host": "smtp.mailgun.org", "port": 587, "security": "starttls", "limit": 1000},
    "ses": {"label": "Amazon SES (change the region in the host)", "host": "email-smtp.us-east-1.amazonaws.com", "port": 587, "security": "starttls", "limit": 5000},
    "resend": {"label": "Resend (user is 'resend', password is your API key)", "host": "smtp.resend.com", "port": 465, "security": "ssl", "limit": 1000},
    "sendgrid": {"label": "SendGrid (user is 'apikey')", "host": "smtp.sendgrid.net", "port": 587, "security": "starttls", "limit": 1000},
    "other": {"label": "Other mail server", "host": "", "port": 587, "security": "starttls", "limit": 400},
}
DEFAULTS = {"from_name": "", "from_email": "", "reply_to": "", "postal_address": "", "site_url": "",
            "smtp_host": "", "smtp_port": 587, "smtp_security": "starttls", "smtp_user": "",
            "daily_limit": 400, "pause_seconds": 1.0, "double_opt_in": True, "accent": "#2e7d32", "preset": "other"}
MAX_BLOCKS, MAX_TEXT, MAX_SUBSCRIBERS = 60, 20000, 50000
_locks = {}
_jobs = {}
_hits = {}


class NewsError(ValueError):
    pass


# ----------------------------------------------------------------------------- storage
def _dir(folder) -> Path:
    path = Path(folder) / DIR
    path.mkdir(exist_ok=True)
    return path


def _atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    os.replace(tmp, path)


@contextlib.contextmanager
def db(folder):
    conn = sqlite3.connect(_dir(folder) / "subscribers.db", timeout=20)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS subscribers(
        id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
        token TEXT UNIQUE NOT NULL, created TEXT NOT NULL, confirmed TEXT, last_confirm_mail TEXT, source TEXT);
    CREATE TABLE IF NOT EXISTS campaigns(
        id INTEGER PRIMARY KEY, subject TEXT NOT NULL, blocks TEXT NOT NULL, status TEXT NOT NULL,
        created TEXT NOT NULL, started TEXT, finished TEXT);
    CREATE TABLE IF NOT EXISTS sends(
        campaign_id INTEGER NOT NULL, subscriber_id INTEGER NOT NULL, status TEXT NOT NULL, error TEXT, at TEXT,
        PRIMARY KEY(campaign_id, subscriber_id));
    """)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load_settings(folder) -> dict:
    values = dict(DEFAULTS)
    try:
        saved = json.loads((Path(folder) / DIR / "settings.json").read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            values.update({k: saved[k] for k in DEFAULTS if k in saved})
    except (OSError, ValueError):
        pass
    return values


def password_set(folder) -> bool:
    return bool(site_secrets.read(folder).get(PASSWORD_KEY))


def save_settings(folder, data: dict, password: str = "") -> None:
    cur = load_settings(folder)
    new = dict(cur)

    def text(key, limit, label, required=False):
        if key in data:
            value = " ".join(str(data.get(key, "")).split())[:limit] if key != "postal_address" else \
                ", ".join(p.strip() for p in str(data.get(key, "")).replace("\r", "").split("\n") if p.strip())[:300]
            if required and not value:
                raise NewsError(f"Enter {label}.")
            new[key] = value
    text("from_name", 80, "the name emails come from")
    text("postal_address", 300, "your postal address")
    text("smtp_host", 200, "the mail server")
    text("smtp_user", 200, "the mail account user name")
    text("preset", 20, "")
    for key in ("from_email", "reply_to"):
        if key in data:
            value = str(data.get(key, "")).strip()
            if value and not EMAIL_RE.match(value):
                raise NewsError("That email address does not look right.")
            new[key] = value
    if "site_url" in data:
        url = str(data.get("site_url", "")).strip().rstrip("/")
        if url and not re.match(r"^https?://[^\s/]+(/[^\s]*)?$", url):
            raise NewsError("The website address must look like https://www.example.com")
        new["site_url"] = url
    if "smtp_port" in data:
        try:
            port = int(data["smtp_port"])
        except (TypeError, ValueError):
            raise NewsError("The mail server port must be a number such as 587.")
        if not 1 <= port <= 65535:
            raise NewsError("The mail server port must be between 1 and 65535.")
        new["smtp_port"] = port
    if "smtp_security" in data:
        if data["smtp_security"] not in ("ssl", "starttls", "none"):
            raise NewsError("Choose how to secure the connection.")
        new["smtp_security"] = data["smtp_security"]
    if new["smtp_security"] == "none" and new["smtp_host"] not in ("127.0.0.1", "localhost"):
        raise NewsError("An unencrypted connection is only allowed to this computer. Choose SSL or STARTTLS.")
    for key, lo, hi, kind in (("daily_limit", 1, 100000, int), ("pause_seconds", 0, 60, float)):
        if key in data:
            try:
                value = kind(data[key])
            except (TypeError, ValueError):
                raise NewsError("Enter a number for the sending limits.")
            if not lo <= value <= hi:
                raise NewsError("The sending limits are out of range.")
            new[key] = value
    if "double_opt_in" in data:
        new["double_opt_in"] = bool(data["double_opt_in"])
    if "accent" in data:
        color = str(data["accent"]).strip()
        if not re.match(r"^#[0-9a-fA-F]{6}$", color):
            raise NewsError("Choose the accent colour with the colour picker.")
        new["accent"] = color
    _atomic(_dir(folder) / "settings.json", json.dumps(new, indent=2) + "\n")
    if password:
        if not 4 <= len(password) <= 200 or re.search(r"[\r\n\x00]", password):
            raise NewsError("That mail password does not look right.")
        values = site_secrets.read(folder)
        values[PASSWORD_KEY] = password
        site_secrets.write(folder, values)


def mail_ready(folder, s=None) -> list:
    """What is still missing before newsletters can be sent (empty list means ready)."""
    s = s or load_settings(folder)
    missing = []
    if not (s["smtp_host"] and s["smtp_user"] and password_set(folder)):
        missing.append("the mail account (server, user name and password)")
    if not s["from_email"]:
        missing.append("the 'from' email address")
    if not s["postal_address"]:
        missing.append("your postal address (the law requires it in every newsletter)")
    if not s["site_url"]:
        missing.append("your website's public address (used in the confirm and unsubscribe links)")
    return missing


# ----------------------------------------------------------------------------- email building
def _http_url(value: str, site_url: str = "", allow_relative=False) -> str:
    value = str(value or "").strip()
    if not value or re.search(r"[\x00-\x20]", value) or len(value) > 2000:
        raise NewsError("A link or picture address is missing or contains spaces.")
    if re.match(r"^https?://", value, re.I):
        return value
    if allow_relative and site_url and not re.match(r"^[a-z][a-z0-9+.-]*:", value, re.I) \
            and not value.startswith(("/", "\\")) and ".." not in value.split("/"):
        return site_url.rstrip("/") + "/" + value
    raise NewsError("Links must start with https:// (pictures from your site may use a path like assets/photo.jpg).")


def clean_blocks(blocks, site_url="") -> list:
    if not isinstance(blocks, list) or not blocks:
        raise NewsError("Add at least one block to the newsletter.")
    if len(blocks) > MAX_BLOCKS:
        raise NewsError(f"A newsletter can have at most {MAX_BLOCKS} blocks.")
    out, total = [], 0
    for b in blocks:
        b = b if isinstance(b, dict) else {}
        kind = b.get("type")
        if kind in ("heading", "text"):
            text = str(b.get("text", "")).replace("\r\n", "\n").strip()
            if not text:
                raise NewsError("A heading or text block is empty. Fill it in or remove it.")
            total += len(text)
            out.append({"type": kind, "text": text[:MAX_TEXT]})
        elif kind == "image":
            out.append({"type": "image", "src": str(b.get("src", "")).strip(),
                        "alt": " ".join(str(b.get("alt", "")).split())[:200],
                        "link": str(b.get("link", "")).strip()})
            _http_url(out[-1]["src"], site_url or "x", allow_relative=bool(site_url))
            if out[-1]["link"]:
                _http_url(out[-1]["link"])
        elif kind == "button":
            text = " ".join(str(b.get("text", "")).split())[:60]
            if not text:
                raise NewsError("A button needs its text.")
            out.append({"type": "button", "text": text, "url": str(b.get("url", "")).strip()})
            _http_url(out[-1]["url"])
        elif kind == "divider":
            out.append({"type": "divider"})
        else:
            raise NewsError("Unknown block type.")
    if total > MAX_TEXT * 2:
        raise NewsError("This newsletter is too long.")
    return out


def _paras(text: str, style: str) -> str:
    return "".join(f'<p style="{style}">{html.escape(p).replace(chr(10), "<br>")}</p>'
                   for p in re.split(r"\n\s*\n", text) if p.strip())


def render(blocks, settings, subject, unsub_url, site_name="") -> tuple:
    """(html, plain text) for one recipient."""
    accent = settings["accent"]
    site_url = settings["site_url"]
    h, t = [], []
    for b in blocks:
        if b["type"] == "heading":
            h.append(f'<h1 style="margin:24px 0 8px;font:700 26px/1.25 Arial,Helvetica,sans-serif;color:#111">{html.escape(b["text"])}</h1>')
            t.append(b["text"].upper() + "\n")
        elif b["type"] == "text":
            h.append(_paras(b["text"], "margin:0 0 16px;font:16px/1.6 Arial,Helvetica,sans-serif;color:#333"))
            t.append(b["text"] + "\n")
        elif b["type"] == "image":
            src = _http_url(b["src"], site_url, allow_relative=True)
            img = (f'<img src="{html.escape(src, quote=True)}" alt="{html.escape(b["alt"], quote=True)}" width="560" '
                   f'style="display:block;width:100%;max-width:560px;height:auto;border:0;margin:16px 0">')
            h.append(f'<a href="{html.escape(b["link"], quote=True)}">{img}</a>' if b["link"] else img)
            if b["alt"]:
                t.append(f"[{b['alt']}]\n")
        elif b["type"] == "button":
            h.append(f'<p style="margin:20px 0"><a href="{html.escape(b["url"], quote=True)}" style="display:inline-block;'
                     f'padding:12px 24px;background:{accent};color:#ffffff;text-decoration:none;border-radius:6px;'
                     f'font:700 16px Arial,Helvetica,sans-serif">{html.escape(b["text"])}</a></p>')
            t.append(f"{b['text']}: {b['url']}\n")
        else:
            h.append('<hr style="border:0;border-top:1px solid #ddd;margin:24px 0">')
            t.append("-----\n")
    foot_t = (f"\n-----\nYou are receiving this because you signed up at {site_url or 'our website'}.\n"
              f"Unsubscribe: {unsub_url}\n{settings['postal_address']}\n")
    foot_h = (f'<p style="margin:24px 0 0;font:13px/1.5 Arial,Helvetica,sans-serif;color:#777">You are receiving this because you '
              f'signed up at {html.escape(site_url or "our website")}.<br><a href="{html.escape(unsub_url, quote=True)}" '
              f'style="color:#777">Unsubscribe</a><br>{html.escape(settings["postal_address"])}</p>')
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(subject)}</title></head><body style="margin:0;padding:0;background:#f4f4f4">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4"><tr><td align="center" style="padding:24px 12px">'
            f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;background:#ffffff;border-radius:8px">'
            f'<tr><td style="padding:24px 32px">{"".join(h)}{foot_h}</td></tr></table></td></tr></table></body></html>')
    return page, "\n".join(t) + foot_t


def _link(settings, action, token) -> str:
    return f"{settings['site_url'].rstrip('/')}/newsletter.html?a={action}&t={token}"


def make_message(settings, to_addr, subject, html_body, text_body, unsub_url=None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = " ".join(subject.split())[:200]
    msg["From"] = formataddr((settings["from_name"], settings["from_email"]))
    msg["To"] = to_addr
    if settings["reply_to"]:
        msg["Reply-To"] = settings["reply_to"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=settings["from_email"].rsplit("@", 1)[-1])
    if unsub_url:
        msg["List-Unsubscribe"] = f"<{unsub_url}>"
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


# ----------------------------------------------------------------------------- SMTP
def connect(folder, settings=None):
    s = settings or load_settings(folder)
    password = site_secrets.read(folder).get(PASSWORD_KEY, "")
    host, port = s["smtp_host"], int(s["smtp_port"])
    if not host:
        raise NewsError("Enter the mail server first.")
    try:
        if s["smtp_security"] == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            if s["smtp_security"] == "starttls":
                server.starttls(context=ssl.create_default_context())
        if s["smtp_user"] and password:
            server.login(s["smtp_user"], password)
        return server
    except smtplib.SMTPAuthenticationError:
        raise NewsError("The mail server did not accept the user name and password. For Gmail use an app password, not your normal one.")
    except (smtplib.SMTPException, OSError) as exc:
        raise NewsError(f"Could not connect to the mail server: {_scrub(exc, password)}")


def _scrub(exc, password="") -> str:
    text = str(exc)[:300]
    return text.replace(password, "[hidden]") if password else text


def test_connection(folder) -> str:
    server = connect(folder)
    with contextlib.suppress(Exception):
        server.quit()
    return "Connected and signed in to the mail server."


def send_test(folder, to_addr, subject, blocks) -> str:
    s = load_settings(folder)
    problems = mail_ready(folder, s)
    if problems:
        raise NewsError("Before sending, fill in: " + "; ".join(problems) + ".")
    if not EMAIL_RE.match(to_addr or ""):
        raise NewsError("Enter the email address to send the test to.")
    blocks = clean_blocks(blocks, s["site_url"])
    unsub = _link(s, "unsubscribe", "test-link-not-active")
    body_html, body_text = render(blocks, s, subject, unsub)
    server = connect(folder, s)
    try:
        server.send_message(make_message(s, to_addr, "[Test] " + subject, body_html, body_text, unsub))
    except smtplib.SMTPException as exc:
        raise NewsError(f"The mail server refused the message: {_scrub(exc)}")
    finally:
        with contextlib.suppress(Exception):
            server.quit()
    return f"Test email sent to {to_addr}."


# ----------------------------------------------------------------------------- public sign-up routes
def _limited(key, limit, seconds=60) -> bool:
    now = time.time()
    recent = [t for t in _hits.get(key, []) if now - t < seconds] + [now]
    _hits[key] = recent
    return len(recent) > limit


def _confirm_mail(folder, s, email, token) -> bool:
    if mail_ready(folder, s):
        return False
    url = _link(s, "confirm", token)
    text = (f"Please confirm your subscription by opening this link:\n{url}\n\n"
            f"If you did not ask for this, ignore this email and you will not be subscribed.\n")
    body = (f'<p style="font:16px/1.6 Arial,sans-serif">Please confirm your subscription:</p><p><a href="{html.escape(url, quote=True)}" '
            f'style="display:inline-block;padding:12px 24px;background:{s["accent"]};color:#fff;text-decoration:none;border-radius:6px;'
            f'font:700 16px Arial,sans-serif">Confirm my subscription</a></p><p style="font:13px Arial,sans-serif;color:#777">'
            f'If you did not ask for this, ignore this email and you will not be subscribed.</p>')
    try:
        server = connect(folder, s)
        try:
            server.send_message(make_message(s, email, "Please confirm your subscription", body, text))
        finally:
            with contextlib.suppress(Exception):
                server.quit()
        return True
    except (NewsError, smtplib.SMTPException, OSError):
        return False


def public(folder, route, body) -> tuple:
    """Handles /api/subscribe, /api/confirm, /api/unsubscribe for visitors. Returns (status, json)."""
    folder = Path(folder)
    body = body if isinstance(body, dict) else {}
    s = load_settings(folder)
    if route == "subscribe":
        if _limited("sub:" + str(folder), 20):
            return 429, {"error": "Too many sign-ups right now. Please try again in a minute."}
        if str(body.get("website", "")).strip():                       # honeypot: only bots fill it in
            return 200, {"message": "Thanks! Check your inbox."}
        email = str(body.get("email", "")).strip()
        if len(email) > 254 or not EMAIL_RE.match(email):
            return 400, {"error": "Please enter a valid email address."}
        email_l = email.lower()
        with db(folder) as conn:
            total = conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
            row = conn.execute("SELECT * FROM subscribers WHERE email=?", (email_l,)).fetchone()
            if row is None:
                if total >= MAX_SUBSCRIBERS:
                    return 400, {"error": "The list is full right now."}
                status = "pending" if s["double_opt_in"] else "active"
                conn.execute("INSERT INTO subscribers(email,status,token,created,confirmed,source) VALUES(?,?,?,?,?,?)",
                             (email_l, status, secrets.token_urlsafe(24), _now(), None if status == "pending" else _now(), "website"))
                row = conn.execute("SELECT * FROM subscribers WHERE email=?", (email_l,)).fetchone()
            elif row["status"] == "unsubscribed":
                status = "pending" if s["double_opt_in"] else "active"
                conn.execute("UPDATE subscribers SET status=?, confirmed=? WHERE id=?",
                             (status, None if status == "pending" else _now(), row["id"]))
                row = conn.execute("SELECT * FROM subscribers WHERE id=?", (row["id"],)).fetchone()
            if row["status"] == "pending":
                last = row["last_confirm_mail"]
                recent = bool(last) and datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) > \
                    datetime.now(timezone.utc) - timedelta(hours=1)
                if not recent and _confirm_mail(folder, s, email_l, row["token"]):
                    conn.execute("UPDATE subscribers SET last_confirm_mail=? WHERE id=?", (_now(), row["id"]))
        # the same answer whether or not the address was already on the list
        return 200, {"message": "Thanks! Check your inbox to confirm your subscription." if s["double_opt_in"]
                     else "Thanks! You're subscribed."}
    if route in ("confirm", "unsubscribe"):
        if _limited("tok:" + str(folder), 60):
            return 429, {"error": "Too many requests. Please try again in a minute."}
        token = str(body.get("token", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token):
            return 400, {"error": "This link is not valid."}
        with db(folder) as conn:
            row = conn.execute("SELECT * FROM subscribers WHERE token=?", (token,)).fetchone()
            if row is None:
                return 400, {"error": "This link is not valid or has expired."}
            if route == "confirm":
                if row["status"] != "active":
                    conn.execute("UPDATE subscribers SET status='active', confirmed=? WHERE id=?", (_now(), row["id"]))
                return 200, {"message": "You're subscribed. Thank you!"}
            conn.execute("UPDATE subscribers SET status='unsubscribed' WHERE id=?", (row["id"],))
            return 200, {"message": "You have been unsubscribed and will not receive any more newsletters."}
    return 404, {"error": "unknown route"}


# ----------------------------------------------------------------------------- owner actions
def overview(folder) -> dict:
    folder = Path(folder)
    s = load_settings(folder)
    with db(folder) as conn:
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM subscribers GROUP BY status")}
        subs = [dict(r) for r in conn.execute(
            "SELECT id,email,status,created,confirmed FROM subscribers ORDER BY id DESC LIMIT 500")]
        camps = [dict(r) for r in conn.execute(
            "SELECT c.id,c.subject,c.status,c.created,c.finished,"
            "(SELECT COUNT(*) FROM sends s WHERE s.campaign_id=c.id AND s.status='sent') sent,"
            "(SELECT COUNT(*) FROM sends s WHERE s.campaign_id=c.id AND s.status='failed') failed,"
            "(SELECT COUNT(*) FROM sends s WHERE s.campaign_id=c.id AND s.status='queued') queued "
            "FROM campaigns c ORDER BY c.id DESC LIMIT 50")]
    index = (folder / "index.html")
    installed = index.is_file() and BLOCK_START in index.read_text(encoding="utf-8", errors="replace")
    s = dict(s)
    return {"settings": s, "password_set": password_set(folder), "missing": mail_ready(folder, s),
            "counts": {"active": counts.get("active", 0), "pending": counts.get("pending", 0),
                       "unsubscribed": counts.get("unsubscribed", 0)},
            "subscribers": subs, "campaigns": camps, "installed": installed, "presets": PRESETS,
            "sending": job_state(folder), "cloud": cloud_on(folder)}


def get_campaign(folder, cid) -> dict:
    with db(folder) as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (int(cid),)).fetchone()
    if row is None:
        raise NewsError("That newsletter was not found.")
    return {"id": row["id"], "subject": row["subject"], "blocks": json.loads(row["blocks"]), "status": row["status"]}


def save_campaign(folder, cid, subject, blocks) -> int:
    s = load_settings(folder)
    subject = " ".join(str(subject or "").split())[:200]
    if not subject:
        raise NewsError("Enter a subject line.")
    blocks = clean_blocks(blocks, s["site_url"])
    with db(folder) as conn:
        if cid:
            row = conn.execute("SELECT status FROM campaigns WHERE id=?", (int(cid),)).fetchone()
            if row is None:
                raise NewsError("That newsletter was not found.")
            if row["status"] != "draft":
                raise NewsError("A newsletter that has been sent cannot be changed. Use Duplicate to make a new one.")
            conn.execute("UPDATE campaigns SET subject=?, blocks=? WHERE id=?", (subject, json.dumps(blocks), int(cid)))
            return int(cid)
        cur = conn.execute("INSERT INTO campaigns(subject,blocks,status,created) VALUES(?,?,?,?)",
                           (subject, json.dumps(blocks), "draft", _now()))
        return cur.lastrowid


def delete_campaign(folder, cid) -> None:
    with db(folder) as conn:
        row = conn.execute("SELECT status FROM campaigns WHERE id=?", (int(cid),)).fetchone()
        if row and row["status"] == "sending":
            raise NewsError("This newsletter is being sent right now.")
        conn.execute("DELETE FROM sends WHERE campaign_id=?", (int(cid),))
        conn.execute("DELETE FROM campaigns WHERE id=?", (int(cid),))


def subscriber_op(folder, op, ident=None, emails="", consent=False) -> str:
    with db(folder) as conn:
        if op in ("remove", "unsubscribe"):
            row = conn.execute("SELECT email,token FROM subscribers WHERE id=?", (int(ident),)).fetchone()
            if row is None:
                raise NewsError("That subscriber was not found.")
            if cloud_on(folder):                       # keep the published site's list in step
                import deploy_cloudflare as cf
                try:
                    if op == "remove":
                        cf.d1_query(folder, "DELETE FROM subscribers WHERE token=?", [row["token"]])
                    else:
                        cf.d1_query(folder, "UPDATE subscribers SET status='unsubscribed' WHERE token=?", [row["token"]])
                except cf.DeployError as exc:
                    raise NewsError("Could not update the published site, so nothing was changed: " + str(exc))
            if op == "remove":
                conn.execute("DELETE FROM subscribers WHERE id=?", (int(ident),))
                return "Removed."
            conn.execute("UPDATE subscribers SET status='unsubscribed' WHERE id=?", (int(ident),))
            return "Marked as unsubscribed."
        if op == "import":
            if not consent:
                raise NewsError("Tick the box to confirm these people agreed to receive your emails.")
            found = set(re.findall(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", str(emails)))
            added = 0
            for e in sorted(found):
                e = e.lower()
                if len(e) > 254 or not EMAIL_RE.match(e):
                    continue
                if conn.execute("SELECT 1 FROM subscribers WHERE email=?", (e,)).fetchone():
                    continue
                conn.execute("INSERT INTO subscribers(email,status,token,created,confirmed,source) VALUES(?,?,?,?,?,?)",
                             (e, "active", secrets.token_urlsafe(24), _now(), _now(), "imported"))
                added += 1
            return f"Added {added} address(es). Addresses that were already on the list were skipped."
    raise NewsError("Unknown action.")


def export_csv(folder) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["email", "status", "signed_up", "confirmed"])
    with db(folder) as conn:
        for r in conn.execute("SELECT email,status,created,confirmed FROM subscribers ORDER BY id"):
            w.writerow([("'" + r["email"]) if r["email"][:1] in "=+-@" else r["email"], r["status"], r["created"], r["confirmed"] or ""])
    return out.getvalue()


def send_confirmations(folder) -> str:
    s = load_settings(folder)
    if cloud_on(folder):
        sync_cloud(folder)
    problems = mail_ready(folder, s)
    if problems:
        raise NewsError("Before sending, fill in: " + "; ".join(problems) + ".")
    sent = 0
    with db(folder) as conn:
        rows = conn.execute("SELECT id,email,token FROM subscribers WHERE status='pending' LIMIT 500").fetchall()
        for r in rows:
            if _confirm_mail(folder, s, r["email"], r["token"]):
                conn.execute("UPDATE subscribers SET last_confirm_mail=? WHERE id=?", (_now(), r["id"]))
                sent += 1
                time.sleep(float(s["pause_seconds"]))
    return f"Sent {sent} confirmation email(s)."


# ----------------------------------------------------------------------------- Cloudflare sign-ups
def cloud_on(folder) -> bool:
    import deploy_cloudflare
    return deploy_cloudflare.cloud_newsletter(folder)


def sync_cloud(folder) -> str:
    """Bring sign-ups, confirmations and unsubscribes collected on the published site down to this computer,
    and tell the published site about people the owner unsubscribed or removed here."""
    import deploy_cloudflare as cf
    folder = Path(folder)
    if not cloud_on(folder):
        raise NewsError("This site has not been published with a newsletter database yet.")
    try:
        remote = cf.d1_query(folder, "SELECT email,status,token,created,confirmed FROM subscribers")
    except cf.DeployError as exc:
        raise NewsError(str(exc))
    added = changed = pushed = 0
    pending_removed = []
    with db(folder) as conn:
        for r in remote:
            email, status, token = str(r["email"]).lower(), r["status"], str(r["token"])
            if status not in ("pending", "active", "unsubscribed") or not EMAIL_RE.match(email):
                continue
            row = conn.execute("SELECT id,status,token FROM subscribers WHERE email=?", (email,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO subscribers(email,status,token,created,confirmed,source) VALUES(?,?,?,?,?,?)",
                             (email, status, token, r.get("created") or _now(), r.get("confirmed"), "cloudflare"))
                added += 1
            elif row["token"] == token:
                if row["status"] != status:
                    if row["status"] == "unsubscribed" and status == "active":
                        # the owner unsubscribed them here: keep that and tell the published site
                        cf.d1_query(folder, "UPDATE subscribers SET status='unsubscribed' WHERE token=?", [token])
                        pushed += 1
                    else:
                        conn.execute("UPDATE subscribers SET status=?, confirmed=? WHERE id=?",
                                     (status, r.get("confirmed"), row["id"]))
                        changed += 1
            else:
                conn.execute("UPDATE subscribers SET token=? WHERE id=?", (token, row["id"]))
    note = f"Synced with the published site: {added} new, {changed} updated"
    if pushed:
        note += f", {pushed} change(s) sent back"
    return note + "."


# ----------------------------------------------------------------------------- sending a newsletter
def job_state(folder) -> dict:
    return dict(_jobs.get(str(Path(folder).resolve()), {"running": False}))


def start_send(folder, cid) -> str:
    folder = Path(folder).resolve()
    key = str(folder)
    s = load_settings(folder)
    problems = mail_ready(folder, s)
    if problems:
        raise NewsError("Before sending, fill in: " + "; ".join(problems) + ".")
    camp = get_campaign(folder, cid)
    blocks = clean_blocks(camp["blocks"], s["site_url"])
    if cloud_on(folder):
        try:
            sync_cloud(folder)          # honour unsubscribes made on the published site before sending
        except NewsError as exc:
            raise NewsError("Could not check the published site for new unsubscribes, so nothing was sent: " + str(exc))
    lock = _locks.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise NewsError("A newsletter is already being sent for this site.")
    try:
        with db(folder) as conn:
            if camp["status"] == "draft":
                conn.execute("INSERT OR IGNORE INTO sends(campaign_id,subscriber_id,status) "
                             "SELECT ?, id, 'queued' FROM subscribers WHERE status='active'", (camp["id"],))
                conn.execute("UPDATE campaigns SET status='sending', started=? WHERE id=?", (_now(), camp["id"]))
            elif camp["status"] not in ("partial", "sending"):
                raise NewsError("This newsletter has already been sent.")
            else:
                conn.execute("UPDATE campaigns SET status='sending' WHERE id=?", (camp["id"],))
            queued = conn.execute("SELECT COUNT(*) FROM sends WHERE campaign_id=? AND status='queued'", (camp["id"],)).fetchone()[0]
        if not queued:
            with db(folder) as conn:
                conn.execute("UPDATE campaigns SET status='sent', finished=? WHERE id=?", (_now(), camp["id"]))
            lock.release()
            raise NewsError("There is nobody to send to yet. Subscribers must be confirmed (active).")
    except Exception:
        if lock.locked() and not _jobs.get(key, {}).get("running"):
            with contextlib.suppress(RuntimeError):
                lock.release()
        raise
    _jobs[key] = {"running": True, "campaign": camp["id"], "sent": 0, "failed": 0, "total": queued, "message": "Starting..."}
    threading.Thread(target=_send_worker, args=(folder, camp, blocks, s, lock), daemon=True).start()
    return f"Sending to {queued} subscriber(s)..."


def _send_worker(folder, camp, blocks, s, lock):
    key = str(folder)
    job = _jobs[key]
    server, stop = None, ""
    try:
        sent_today = 0
        with db(folder) as conn:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            sent_today = conn.execute("SELECT COUNT(*) FROM sends WHERE status='sent' AND at LIKE ?", (day + "%",)).fetchone()[0]
        server = connect(folder, s)
        while True:
            with db(folder) as conn:
                row = conn.execute(
                    "SELECT s.subscriber_id id, u.email, u.token, u.status FROM sends s JOIN subscribers u ON u.id=s.subscriber_id "
                    "WHERE s.campaign_id=? AND s.status='queued' ORDER BY s.subscriber_id LIMIT 1", (camp["id"],)).fetchone()
                if row is None:
                    break
                if row["status"] != "active":
                    conn.execute("UPDATE sends SET status='skipped', at=? WHERE campaign_id=? AND subscriber_id=?",
                                 (_now(), camp["id"], row["id"]))
                    continue
            if sent_today >= int(s["daily_limit"]):
                stop = (f"Stopped at your daily limit of {s['daily_limit']} emails. Press Continue sending tomorrow "
                        f"to send the rest.")
                break
            unsub = _link(s, "unsubscribe", row["token"])
            body_html, body_text = render(blocks, s, camp["subject"], unsub)
            status, error = "sent", None
            try:
                try:
                    server.send_message(make_message(s, row["email"], camp["subject"], body_html, body_text, unsub))
                except smtplib.SMTPServerDisconnected:
                    server = connect(folder, s)
                    server.send_message(make_message(s, row["email"], camp["subject"], body_html, body_text, unsub))
            except smtplib.SMTPRecipientsRefused as exc:
                status, error = "failed", _scrub(exc)
            except (smtplib.SMTPException, OSError) as exc:
                status, error = "failed", _scrub(exc, site_secrets.read(folder).get(PASSWORD_KEY, ""))
                if isinstance(exc, (smtplib.SMTPSenderRefused, smtplib.SMTPDataError)) and job["failed"] >= 3 and job["sent"] == 0:
                    stop = "The mail server keeps refusing messages: " + error
            with db(folder) as conn:
                conn.execute("UPDATE sends SET status=?, error=?, at=? WHERE campaign_id=? AND subscriber_id=?",
                             (status, error, _now(), camp["id"], row["id"]))
            if status == "sent":
                job["sent"] += 1
                sent_today += 1
            else:
                job["failed"] += 1
            job["message"] = f"Sent {job['sent']} of {job['total']}"
            if stop:
                break
            time.sleep(float(s["pause_seconds"]))
    except NewsError as exc:
        stop = str(exc)
    except Exception as exc:                                    # never leave the job stuck
        stop = "Unexpected error: " + _scrub(exc)
    finally:
        if server is not None:
            with contextlib.suppress(Exception):
                server.quit()
        with db(folder) as conn:
            left = conn.execute("SELECT COUNT(*) FROM sends WHERE campaign_id=? AND status='queued'", (camp["id"],)).fetchone()[0]
            if left:
                conn.execute("UPDATE campaigns SET status='partial' WHERE id=?", (camp["id"],))
            else:
                conn.execute("UPDATE campaigns SET status='sent', finished=? WHERE id=?", (_now(), camp["id"]))
        job["running"] = False
        job["message"] = stop or f"Finished: {job['sent']} sent, {job['failed']} failed."
        job["stopped"] = bool(stop)
        lock.release()


# ----------------------------------------------------------------------------- put the sign-up form on the site
SIGNUP_BLOCK = f"""{BLOCK_START}
<section class="footer-newsletter news-signup" aria-label="Newsletter signup">
  <form class="news-form" data-news-form novalidate>
    <label for="news-email" class="footer-newsletter__label">Stay updated</label>
    <div class="news-row">
      <input type="email" id="news-email" name="email" class="footer-newsletter__input" placeholder="Enter your email" autocomplete="email" required maxlength="254">
      <button type="submit" class="news-button">Subscribe</button>
    </div>
    <input type="text" name="website" class="news-hp" tabindex="-1" autocomplete="off" aria-hidden="true">
    <p class="news-note">News and updates by email. Unsubscribe at any time.</p>
    <p class="news-status" role="status" aria-live="polite"></p>
  </form>
  <script src="newsletter.js" defer data-news></script>
</section>
{BLOCK_END}"""

NEWS_CSS = f"""
{CSS_MARK}
.news-row{{display:flex;flex-wrap:wrap;gap:.5rem}}
.news-row input{{flex:1 1 14rem;min-width:0}}
.news-button{{padding:.6rem 1.1rem;border:0;border-radius:999px;cursor:pointer;font:inherit;font-weight:600;background:var(--color-button-bg,#222);color:var(--color-button-text,#fff)}}
.news-hp{{position:absolute!important;left:-9999px!important;width:1px;height:1px;opacity:0}}
.news-note{{margin:.5rem 0 0;font-size:.85em;opacity:.8}}
.news-status{{margin:.4rem 0 0;font-weight:600;min-height:1.2em}}
.news-status.is-error{{color:#b00020}}
.news-page{{max-width:40rem;margin:0 auto;padding:3rem 5%}}
"""

NEWS_JS = r"""/* Newsletter sign-up, confirm and unsubscribe. Generated by newsletter.py; safe to regenerate. */
(function () {
  'use strict';
  function post(route, body) {
    return fetch('api/' + route, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (!r.ok) { throw new Error(d.error || 'Something went wrong. Please try again.'); }
          return d;
        });
      });
  }
  Array.prototype.forEach.call(document.querySelectorAll('[data-news-form]'), function (form) {
    var status = form.querySelector('.news-status');
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var email = form.elements.email.value.trim();
      if (!email) { status.className = 'news-status is-error'; status.textContent = 'Please enter your email address.'; return; }
      status.className = 'news-status'; status.textContent = 'One moment...';
      post('subscribe', { email: email, website: form.elements.website ? form.elements.website.value : '' })
        .then(function (d) { status.textContent = d.message; form.elements.email.value = ''; })
        .catch(function (err) { status.className = 'news-status is-error'; status.textContent = err.message; });
    });
  });
  var box = document.getElementById('news-action');
  if (box) {
    var q = new URLSearchParams(location.search), action = q.get('a'), token = q.get('t') || '';
    var msg = document.getElementById('news-message');
    function say(t, bad) { msg.textContent = t; msg.className = bad ? 'news-status is-error' : 'news-status'; }
    if (action === 'confirm') {
      say('Confirming...');
      post('confirm', { token: token }).then(function (d) { say(d.message); }).catch(function (e) { say(e.message, true); });
    } else if (action === 'unsubscribe') {
      say('Press the button to stop receiving our newsletter.');
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'news-button'; b.textContent = 'Yes, unsubscribe me';
      b.addEventListener('click', function () {
        b.disabled = true;
        post('unsubscribe', { token: token }).then(function (d) { say(d.message); b.remove(); })
          .catch(function (e) { say(e.message, true); b.disabled = false; });
      });
      box.appendChild(b);
    } else { say('This link is not valid.', true); }
  }
})();
"""


def install(folder) -> list:
    """Add the sign-up form to the site (retroactive: works on any site that has an index.html)."""
    import site_editor
    folder = Path(folder).resolve()
    index_path = folder / "index.html"
    src = index_path.read_text(encoding="utf-8")
    notes = []
    doc = site_editor.parse(src)
    _body, _main, _header, footer, _parent, _seq = site_editor._structure(doc)
    if BLOCK_START in src:
        new = re.sub(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END), lambda m: SIGNUP_BLOCK, src, flags=re.S)
        notes.append("Sign-up form refreshed.")
    else:
        target = None
        if footer is not None:
            target = next((n for n in footer.walk() if n is not footer and n.close_end is not None and (
                any("newsletter" in c.lower() for c in n.cls()) or "newsletter" in n.attrs.get("id", "").lower())
                and n.tag in ("section", "div", "form", "aside")), None)
        if target is not None:
            new = src[:target.start] + SIGNUP_BLOCK + src[target.close_end:]
            notes.append("Replaced the old newsletter box in the footer with a working sign-up form.")
        elif footer is not None and footer.close_start is not None:
            new = src[:footer.close_start] + SIGNUP_BLOCK + "\n" + src[footer.close_start:]
            notes.append("Added a sign-up form to the footer.")
        else:
            raise NewsError("This site has no footer to put the sign-up form in.")
        backup = folder / "index.html.before_newsletter"
        if not backup.exists():
            backup.write_text(src, encoding="utf-8", newline="\n")
    if new != src:
        site_editor._write(index_path, new)
    css = folder / "styles.css"
    if css.is_file():
        text = css.read_text(encoding="utf-8")
        if CSS_MARK not in text:
            site_editor._write(css, text.rstrip() + "\n" + NEWS_CSS)
    site_editor._write(folder / "newsletter.js", NEWS_JS)
    _write_page(folder)
    changed = site_editor.sync_shared(folder)
    notes.append(f"The footer was copied to {len(changed)} other page(s)." if changed else "")
    return [n for n in notes if n]


def _write_page(folder: Path) -> None:
    import site_editor
    index = (folder / "index.html").read_text(encoding="utf-8")
    doc = site_editor.parse(index)
    body, _m, header, footer, _p, _s = site_editor._structure(doc)
    head = doc.find("head")
    head_inner = index[head.open_end:head.close_start] if head is not None and head.close_start is not None else \
        '  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1">\n  <link rel="stylesheet" href="styles.css">\n'
    site = site_editor._title_of(index, folder.name)
    head_inner = re.sub(r"<title>.*?</title>", f"<title>Newsletter | {html.escape(site, quote=False)}</title>", head_inner, count=1, flags=re.S | re.I)
    head_inner = re.sub(r'\s*<meta\b[^>]*name\s*=\s*["\']description["\'][^>]*>', "", head_inner, flags=re.I)
    head_inner += '\n  <meta name="robots" content="noindex">'
    ids = site_editor._ids(index)
    hdr = site_editor._for_subpage(index[header.start:header.close_end], ids) if header else ""
    ftr = site_editor._for_subpage(index[footer.start:footer.close_end], ids) if footer else ""
    page = f"""<!doctype html>
<html lang="en">
<head>
{head_inner.strip(chr(10))}
</head>
<body>
{PAGE_MARK}
{hdr}
<main id="main" class="news-page">
  <h1>Newsletter</h1>
  <div id="news-action"><p id="news-message" class="news-status" role="status" aria-live="polite"></p></div>
  <p><a href="index.html">&larr; Back to the website</a></p>
</main>
{ftr}
</body>
</html>
"""
    site_editor._write(folder / "newsletter.html", page)
