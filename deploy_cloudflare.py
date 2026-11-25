#!/usr/bin/env python3
"""Publish a generated site to Cloudflare Pages from the Studio.

What it does
  1. checks your Cloudflare API token and account (a safe read-only call)
  2. copies only the PUBLIC files of the site into a clean temporary folder (pages, styles, scripts, pictures);
     it never copies hidden files, keys, the subscriber list, items.csv, backups or Python files
  3. uploads that folder with Cloudflare's own `wrangler` tool (needs Node.js; it is fetched automatically with npx)
  4. optionally attaches your own domain name to the Pages project

Your account ID is saved in <site>/.deploy/cloudflare.json and the API token in the site's hidden key file
(.shop_secrets.env), the same place as the other per-site keys. Neither is ever put in a web page.

Cloudflare Pages hosts files, so the parts of a site that need a server are published as Pages Functions
(cf_worker.py): card checkout and USPS prices use Stripe and USPS directly, and newsletter sign-ups are kept in a
Cloudflare D1 database. Newsletters themselves are still written and sent from the Studio (newsletter.sync_cloud
pulls the new subscribers down first).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import site_secrets
import cf_worker

TOKEN_KEY = "CLOUDFLARE_API_TOKEN"
DIR = ".deploy"
API = "https://api.cloudflare.com/client/v4"
PUBLIC_SUFFIXES = {".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico",
                   ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".webm", ".webmanifest", ".xml", ".pdf"}
EXTRA_FILES = {"robots.txt", "credits.txt"}
SKIP_NAMES = re.compile(r"(\.before_|\.bak$|\.part$|\.tmp$)", re.I)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,56}[a-z0-9]$")
ACCOUNT_RE = re.compile(r"^[0-9a-f]{32}$")
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


def _node_runtime():
    """Return (npx path, Node bin directory) for a Node version Wrangler supports."""
    candidates = []
    # Prefer nvm installations, including those installed for the current user.
    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.is_dir():
        for bin_dir in nvm_root.glob("*/bin"):
            candidates.append(bin_dir)
    # Also consider the Node/npm pair already exposed to the Studio.
    current_npx = shutil.which("npx")
    if current_npx:
        candidates.append(Path(current_npx).resolve().parent)

    seen = set()
    valid = []
    for bin_dir in candidates:
        try:
            bin_dir = bin_dir.resolve()
            if bin_dir in seen:
                continue
            seen.add(bin_dir)
            node = bin_dir / "node"
            npx = bin_dir / "npx"
            if not node.is_file() or not npx.is_file():
                continue
            result = subprocess.run([str(node), "--version"], capture_output=True,
                                    text=True, timeout=10)
            match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", result.stdout.strip())
            if result.returncode != 0 or not match:
                continue
            version = tuple(map(int, match.groups()))
            if version >= (22, 12, 0):
                valid.append((version, str(npx), str(bin_dir)))
        except (OSError, subprocess.SubprocessError):
            continue
    if not valid:
        return None, None
    _version, npx, bin_dir = max(valid, key=lambda item: item[0])
    return npx, bin_dir


def _node_environment():
    npx, bin_dir = _node_runtime()
    if not npx:
        raise DeployError("Cloudflare publishing requires Node.js 22.12.0 or newer. "
                          "Install it with nvm (nvm install 22) and restart the Studio.")
    env = dict(os.environ)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return npx, env


class DeployError(ValueError):
    pass


def _cfg_path(folder) -> Path:
    d = Path(folder) / DIR
    d.mkdir(exist_ok=True)
    return d / "cloudflare.json"


def load_config(folder) -> dict:
    try:
        data = json.loads(_cfg_path(folder).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(folder, account_id="", project="", token="", domain=None) -> None:
    cfg = load_config(folder)
    if account_id:
        if not ACCOUNT_RE.match(account_id.strip().lower()):
            raise DeployError("The account ID is 32 letters and digits. Copy it from the right side of the Cloudflare dashboard home page.")
        cfg["account_id"] = account_id.strip().lower()
    if project:
        project = project.strip().lower()
        if not NAME_RE.match(project):
            raise DeployError("The project name can use lowercase letters, digits and hyphens (for example plantmantoo).")
        cfg["project"] = project
    if domain is not None:
        domain = domain.strip().lower()
        if domain and not DOMAIN_RE.match(domain):
            raise DeployError("Enter the domain like www.example.com (no https://).")
        cfg["domain"] = domain
    path = _cfg_path(folder)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    if token:
        token = token.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,80}", token):
            raise DeployError("That does not look like a Cloudflare API token (letters, digits, - and _ only).")
        values = site_secrets.read(folder)
        values[TOKEN_KEY] = token
        site_secrets.write(folder, values)


def token_for(folder) -> str:
    return site_secrets.read(folder).get(TOKEN_KEY, "")


def status(folder) -> dict:
    cfg = load_config(folder)
    token = token_for(folder)
    return {"account_id": cfg.get("account_id", ""), "project": cfg.get("project", ""), "domain": cfg.get("domain", ""),
            "token_set": bool(token), "token_masked": ("****" + token[-4:]) if token else "",
            "last_url": cfg.get("last_url", ""), "last_deploy": cfg.get("last_deploy", ""),
            "node": bool(_node_runtime()[0]), "needs_server": needs_server(folder),
            "has_database": bool(cfg.get("d1_id"))}


def _call(method, path, token, body=None):
    req = urllib.request.Request(API + path, method=method, headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json", "User-Agent": "website-studio"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8", "replace"))
        except ValueError:
            data = {}
        errs = "; ".join(str(e.get("message", "")) for e in data.get("errors", []) if isinstance(e, dict))
        if exc.code in (401, 403):
            raise DeployError("Cloudflare did not accept the token for this account. Check that the token has "
                              "'Cloudflare Pages: Edit' permission and the right account." + (f" ({errs})" if errs else ""))
        raise DeployError(f"Cloudflare said: {errs or exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        raise DeployError(f"Could not reach Cloudflare: {exc}")


def check_connection(folder) -> str:
    cfg, token = load_config(folder), token_for(folder)
    if not (cfg.get("account_id") and token):
        raise DeployError("Save your account ID and API token first.")
    _call("GET", f"/accounts/{cfg['account_id']}/pages/projects?per_page=1", token)
    return "Connected to Cloudflare. The token can manage Pages on this account."


def uses_cart(folder) -> bool:
    return (Path(folder) / "cart.html").is_file()


def uses_newsletter(folder) -> bool:
    index = Path(folder) / "index.html"
    return index.is_file() and "studio-news:start" in index.read_text(encoding="utf-8", errors="replace")


def needs_server(folder) -> list:
    """Things the person should know before publishing (not blockers)."""
    out = []
    if uses_cart(folder):
        secrets = site_secrets.read(folder)
        import plugin_stripe, plugin_usps
        if not secrets.get(plugin_stripe.KEY_NAME):
            out.append("no Stripe key is saved for this site, so shoppers will not be able to pay (use the Payments button)")
        if not secrets.get(plugin_usps.SECRET_NAME):
            out.append("no USPS details are saved for this site, so checkout will not offer shipping prices (use the Shipping button)")
    if uses_newsletter(folder):
        out.append("newsletter sign-ups are collected online, but you send newsletters from the Studio (press Sync first)")
    return out


def build_bundle(folder, dest: Path) -> list:
    """Copy the public files of the site into dest. Returns the relative paths copied."""
    folder = Path(folder).resolve()
    copied = []
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if not path.is_file() or any(p.startswith(".") for p in rel.parts) or SKIP_NAMES.search(path.name):
            continue
        if path.is_symlink() or folder not in path.resolve().parents:
            continue
        if path.suffix.lower() in PUBLIC_SUFFIXES or (len(rel.parts) == 1 and path.name in EXTRA_FILES):
            if path.suffix.lower() == ".xml" and path.name != "feed.xml" and path.name != "sitemap.xml":
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append(str(rel))
    if "index.html" not in copied:
        raise DeployError("The site has no index.html to publish.")
    (dest / "_headers").write_text("/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n", encoding="utf-8")
    return copied


def deploy(folder, log=print) -> str:
    folder = Path(folder).resolve()
    cfg, token = load_config(folder), token_for(folder)
    if not (cfg.get("account_id") and cfg.get("project") and token):
        raise DeployError("Save your account ID, project name and API token first.")
    npx, node_env = _node_environment()
    log("[cloudflare] Checking the connection ...")
    log("[cloudflare] " + check_connection(folder))
    with tempfile.TemporaryDirectory(prefix="studio-deploy-") as tmp:
        bundle = Path(tmp) / "site"
        bundle.mkdir()
        files = build_bundle(folder, bundle)
        log(f"[cloudflare] Prepared {len(files)} public file(s). Keys, subscriber lists and backups are not included.")
        for warning in needs_server(folder):
            log(f"[cloudflare] Note: {warning}")
        env = dict(node_env, CLOUDFLARE_API_TOKEN=token, CLOUDFLARE_ACCOUNT_ID=cfg["account_id"],
                   WRANGLER_SEND_METRICS="false", CI="true")
        project = cfg["project"]
        wrangler = [npx, "--yes", "wrangler@4"]

        def run(args, step, stdin=None, allow=None):
            log("[cloudflare] " + step)
            try:
                proc = subprocess.run(wrangler + args, cwd=tmp, env=env, capture_output=True, text=True,
                                      timeout=900, input=stdin)
            except subprocess.TimeoutExpired:
                raise DeployError("Publishing took too long and was stopped. Try again.")
            out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).replace(token, "[hidden]")
            if proc.returncode != 0 and not (allow and re.search(allow, out, re.I)):
                tail = "\n".join(out.strip().splitlines()[-12:])
                raise DeployError("Cloudflare's upload tool reported a problem:\n" + tail)
            return proc.returncode, out

        run(["pages", "project", "create", project, "--production-branch", "main"],
            "Making sure the Pages project exists ...", allow=r"already exists|8000002")
        dynamic = uses_cart(folder) or uses_newsletter(folder)
        if dynamic:
            import shop_cart
            items, _w = shop_cart.load_items(folder)
            fn = Path(tmp) / "functions"
            (fn / "api").mkdir(parents=True)
            (fn / "_lib").mkdir()
            (fn / "api" / "[[route]].js").write_text(cf_worker.ROUTE_JS, encoding="utf-8")
            (fn / "_lib" / "catalog.js").write_text(cf_worker.catalog_js(items, shop_cart.get_currency(folder)), encoding="utf-8")
            toml = [f'name = "{project}"', 'pages_build_output_dir = "./site"', 'compatibility_date = "2025-01-01"']
            try:
                import newsletter
                double = newsletter.load_settings(folder)["double_opt_in"]
            except Exception:
                double = True
            toml += ["", "[vars]", f'DOUBLE_OPT_IN = "{1 if double else 0}"']
            if uses_newsletter(folder):
                db_id = cfg.get("d1_id", "")
                db_name = f"{project}-db"
                if not db_id:
                    rc, out = run(["d1", "create", db_name], "Creating the newsletter database ...", allow=r"already exists")
                    found = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", out)
                    if not found:
                        rc, out = run(["d1", "info", db_name, "--json"], "Looking up the newsletter database ...")
                        found = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", out)
                    if not found:
                        raise DeployError("Could not find the newsletter database id. Check the token has D1: Edit permission.")
                    db_id = found[0]
                    cfg = load_config(folder)
                    cfg["d1_id"], cfg["d1_name"] = db_id, db_name
                    _cfg_path(folder).write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
                toml += ["", "[[d1_databases]]", 'binding = "DB"', f'database_name = "{cfg.get("d1_name", db_name)}"',
                         f'database_id = "{db_id}"']
            (Path(tmp) / "wrangler.toml").write_text("\n".join(toml) + "\n", encoding="utf-8")
            if uses_newsletter(folder):
                (Path(tmp) / "schema.sql").write_text(cf_worker.SCHEMA_SQL, encoding="utf-8")
                run(["d1", "execute", cfg.get("d1_name", f"{project}-db"), "--remote", "--file=schema.sql"],
                    "Preparing the newsletter database ...")
            secrets = site_secrets.read(folder)
            import plugin_stripe, plugin_usps
            wanted = {"STRIPE_SECRET_KEY": secrets.get(plugin_stripe.KEY_NAME, "")}
            if all(secrets.get(k) for k in (plugin_usps.ID_NAME, plugin_usps.SECRET_NAME, plugin_usps.ZIP_NAME)):
                wanted.update({"USPS_CLIENT_ID": secrets[plugin_usps.ID_NAME], "USPS_CLIENT_SECRET": secrets[plugin_usps.SECRET_NAME],
                               "USPS_ORIGIN_ZIP": secrets[plugin_usps.ZIP_NAME]})
            for name, value in wanted.items():
                if value:
                    run(["pages", "secret", "put", name, "--project-name", project], f"Saving {name} securely on Cloudflare ...",
                        stdin=value + "\n")
        rc, out = run(["pages", "deploy", str(bundle), "--project-name", project, "--branch", "main", "--commit-dirty=true"],
                      "Uploading to Cloudflare ...")
        found = re.findall(r"https://[A-Za-z0-9.-]+\.pages\.dev\S*", out)
        url = found[-1].rstrip(".,)") if found else f"https://{project}.pages.dev"
    from datetime import datetime
    cfg = load_config(folder)
    cfg["last_url"], cfg["last_deploy"] = url, datetime.now().strftime("%Y-%m-%d %H:%M")
    _cfg_path(folder).write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    log(f"[cloudflare] Published: {url}")
    return url


def d1_query(folder, sql: str, params=None) -> list:
    """Run one SQL statement on this site's Cloudflare D1 database (newsletter sign-ups). Returns the rows."""
    cfg, token = load_config(folder), token_for(folder)
    if not (cfg.get("account_id") and cfg.get("d1_id") and token):
        raise DeployError("This site has no Cloudflare newsletter database yet. Publish it first.")
    data = _call("POST", f"/accounts/{cfg['account_id']}/d1/database/{cfg['d1_id']}/query", token,
                 {"sql": sql, "params": list(params or [])})
    result = data.get("result") or []
    return (result[0].get("results") if result and isinstance(result[0], dict) else []) or []


def cloud_newsletter(folder) -> bool:
    cfg = load_config(folder)
    return bool(cfg.get("d1_id") and cfg.get("account_id") and token_for(folder))


def add_domain(folder) -> str:
    cfg, token = load_config(folder), token_for(folder)
    domain = cfg.get("domain", "")
    if not (cfg.get("account_id") and cfg.get("project") and token):
        raise DeployError("Save your account ID, project name and API token first.")
    if not domain:
        raise DeployError("Enter your domain name first.")
    try:
        _call("POST", f"/accounts/{cfg['account_id']}/pages/projects/{cfg['project']}/domains", token, {"name": domain})
    except DeployError as exc:
        if "already" in str(exc).lower():
            return f"{domain} is already attached to this project."
        raise
    return (f"{domain} was added. If the domain's DNS is on Cloudflare it connects automatically within a few minutes; "
            f"otherwise add a CNAME record pointing {domain} to {cfg['project']}.pages.dev at your DNS provider.")
