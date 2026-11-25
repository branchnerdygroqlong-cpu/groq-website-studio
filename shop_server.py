#!/usr/bin/env python3
"""Run one generated site as a working shop (cart + Stripe checkout + USPS prices).

    python shop_server.py projects/my-site
    python shop_server.py projects/my-site --port 8000
    python shop_server.py projects/my-site --host 0.0.0.0 --port 8000   (visible on your network)

It serves the site's files and the four shop API routes (see shop_api.py). Your API keys are
read from .env in this folder and are never part of any page it serves. Dot-files, scripts,
spreadsheets, backups and settings files are never served.

Taking real payments from the public internet needs HTTPS in front of this server (a hosting
service or a reverse proxy such as Caddy or nginx) and SHOP_PUBLIC_URL=https://your-domain
saved in .env, so Stripe sends shoppers back to the right address. Until then, test locally
with a Stripe TEST key.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import shop_api
import secrets_env

BLOCKED_SUFFIXES = {".py", ".csv", ".json", ".env", ".part", ".tmp", ".bak", ".log", ".txt"}
ALLOWED_TEXT = {"credits.txt", "robots.txt"}
MAX_BODY = 64 * 1024


def make_handler(site: Path):
    site = site.resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ShopServer"

        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _base(self):
            return shop_api.public_base("http", self.headers.get("Host", "localhost"))

        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path.startswith("/api/"):
                status, data = shop_api.handle(site, path[5:], {}, self._base())
                return self._send(status, data)
            relative = path.lstrip("/") or "index.html"
            target = (site / relative).resolve()
            parts = Path(relative).parts
            if (site not in target.parents and target != site) or any(p.startswith(".") for p in parts) \
                    or "polish_history" in parts:
                return self._send(403, {"error": "forbidden"})
            if target.is_dir():
                target = target / "index.html"
            if not target.is_file() or (target.suffix.lower() in BLOCKED_SUFFIXES
                                        and target.name.lower() not in ALLOWED_TEXT) \
                    or ".before" in target.name:
                return self._send(404, {"error": "not found"})
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        def do_POST(self):
            path = urlparse(self.path).path
            if not path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if not (self.headers.get("Content-Type", "").split(";")[0].strip() == "application/json"):
                return self._send(415, {"error": "JSON only"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > MAX_BODY:
                    return self._send(413, {"error": "too large"})
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send(400, {"error": "invalid request"})
            status, data = shop_api.handle(site, path[5:], body, self._base())
            self._send(status, data)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Serve a generated site as a shop.")
    parser.add_argument("folder")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    site = Path(args.folder)
    if not (site / "index.html").is_file():
        sys.exit(f"{site} has no index.html")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(site))
    print(f"Serving {site} at http://{args.host}:{args.port}/   (Ctrl-C to stop)")
    print(f"Stripe: {'ready' if shop_api.capabilities()['stripe'] else 'not set up (python plugin_stripe.py setup)'}"
          f"   USPS: {'ready' if shop_api.capabilities()['usps'] else 'not set up (python plugin_usps.py setup)'}")
    if args.host not in ("127.0.0.1", "localhost") and not secrets_env.get("SHOP_PUBLIC_URL"):
        print("Note: set SHOP_PUBLIC_URL in .env so Stripe returns shoppers to your public address.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
