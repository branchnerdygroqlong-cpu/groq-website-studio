#!/usr/bin/env python3
"""Groq Website Studio - local web interface."""
import base64
import json
import mimetypes
import os
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE = Path(__file__).resolve().parent
os.chdir(BASE)

try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
except Exception:
    pass

from studio_core.agent_runtime import WebsiteAgents  # noqa: E402
from studio_core.config import PROJECTS_DIR  # noqa: E402
from studio_core.css_polisher import CSSPolisher  # noqa: E402
from studio_core.orchestrator import ProjectOrchestrator, name_from_brief  # noqa: E402
from studio_core.page_expander import PageExpander  # noqa: E402
from studio_core import site_extras  # noqa: E402
import plugin_stripe  # noqa: E402
import plugin_usps  # noqa: E402
import secrets_env  # noqa: E402
import shop_api  # noqa: E402
import site_editor  # noqa: E402
import site_secrets  # noqa: E402
import newsletter  # noqa: E402
import blog  # noqa: E402
import deploy_cloudflare  # noqa: E402
import shop_cart  # noqa: E402

PROJECTS = (BASE / PROJECTS_DIR).resolve()
PROJECTS.mkdir(parents=True, exist_ok=True)


class Job:
    def __init__(self, job_id):
        self.id = job_id
        self.state = "running"
        self.pending = "failed"   # becomes the visible state once the lock is released
        self.lines = []
        self._buf = ""
        self.site_url = None
        self.error = None
        self.lock = threading.Lock()

    def feed(self, text):
        with self.lock:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self.lines.append(line.rstrip())

    def log(self, line):
        self.feed(line + "\n")


class _StdoutRouter:
    def __init__(self, real):
        self.real = real
        self.local = threading.local()

    def write(self, text):
        job = getattr(self.local, "job", None)
        if job is not None:
            job.feed(text)
        return self.real.write(text)

    def flush(self):
        self.real.flush()

    def isatty(self):
        return False


ROUTER = _StdoutRouter(sys.stdout)
sys.stdout = ROUTER

JOBS = {}
BUILD_LOCK = threading.Lock()


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "new-project"


def clean_brief(text):
    """Drop a pasted shell-style wrapper such as  BRIEF="..."  around the description."""
    text = text.strip()
    match = re.match(r"""^[A-Za-z_][A-Za-z0-9_]*\s*=\s*(["'])(.*)\1\s*$""", text, re.S)
    return (match.group(2) if match else text).strip()


def _finish(job, folder):
    job.site_url = f"/sites/{folder}/index.html"
    job.pending = "done"


def _find_project(app, folder):
    project = next(
        (p for p in app.store.list_projects() if Path(p["root_path"]).name == folder),
        None,
    )
    if not project:
        raise RuntimeError(f"No project found for folder '{folder}'.")
    return project


def run_job(job, name, business_type, brief, expand=False, settings=None):
    ROUTER.local.job = job
    try:
        app = ProjectOrchestrator()
        project = app.create_project(name, business_type, brief)
        job.log(f"Created project #{project['id']}: {project['name']}")
        WebsiteAgents(project, store=app.store).run(brief)
        folder = Path(project["root_path"]).name
        job.log("Homepage built.")
        shop_opt = (settings or {}).get("shop")
        if expand and (shop_opt.get("enabled") if isinstance(shop_opt, dict) else shop_opt):
            # tell the page expander a shop is coming, so a "Shop" link is sent to shop.html
            site_extras.save_settings(Path(project["root_path"]), {"email": "", "social": {}, "shop": {"enabled": True}})
        if expand:
            job.log("[pages] Creating the pages the links point to ...")
            try:
                PageExpander(project, store=app.store).run()
            except Exception as exc:
                job.log(f"[pages] Page creation failed: {exc}")
        if settings is not None:
            try:
                site_extras.finalize(Path(project["root_path"]), settings, log=job.log)
            except Exception as exc:
                job.log(f"[extras] Could not add the extras: {exc}")
        _finish(job, folder)
        job.log("Build complete.")
    except Exception as exc:
        job.error = str(exc)
        job.pending = "failed"
        job.log(f"ERROR: {exc}")
    finally:
        ROUTER.local.job = None
        BUILD_LOCK.release()
        job.state = job.pending


def run_expand_job(job, folder):
    ROUTER.local.job = job
    try:
        app = ProjectOrchestrator()
        project = _find_project(app, folder)
        job.log(f"[pages] Adding linked pages to {project['name']} ...")
        PageExpander(project, store=app.store).run()
        _finish(job, folder)
        job.log("Done.")
    except Exception as exc:
        job.error = str(exc)
        job.pending = "failed"
        job.log(f"ERROR: {exc}")
    finally:
        ROUTER.local.job = None
        BUILD_LOCK.release()
        job.state = job.pending


EXTRAS_ACTIONS = ("contacts", "shop", "remove-contacts", "remove-shop")


def run_extras_job(job, folder, action, settings):
    ROUTER.local.job = job
    try:
        app = ProjectOrchestrator()
        project = _find_project(app, folder)
        root = Path(project["root_path"])
        if action == "contacts":
            job.log(f"[contacts] Adding contact details to {project['name']} ...")
            site_extras.set_contacts(root, settings, log=job.log)
        elif action == "shop":
            job.log(f"[shop] Adding the shopping cart to {project['name']} ...")
            site_extras.set_shop(root, log=job.log)
        elif action == "remove-contacts":
            site_extras.remove_contacts(root, log=job.log)
        elif action == "remove-shop":
            site_extras.remove_shop(root, log=job.log)
        else:
            raise ValueError("Unknown action.")
        _finish(job, folder)
        job.log("Done.")
    except Exception as exc:
        job.error = str(exc)
        job.pending = "failed"
        job.log(f"ERROR: {exc}")
    finally:
        ROUTER.local.job = None
        BUILD_LOCK.release()
        job.state = job.pending


def run_deploy_job(job, folder):
    try:
        job.log("[cloudflare] Publishing this site to Cloudflare Pages ...")
        deploy_cloudflare.deploy(PROJECTS / folder, log=job.log)
        job.site_url = f"/sites/{folder}/index.html"
        job.pending = "done"
        job.log("Done.")
    except Exception as exc:
        job.error = str(exc)
        job.pending = "failed"
        job.log(f"ERROR: {exc}")
    finally:
        BUILD_LOCK.release()
        job.state = job.pending


def integrations_status():
    """What the page may know about saved keys: whether they exist and the last 4 characters."""
    return {
        "stripe": {"set": plugin_stripe.configured(), "masked": secrets_env.mask(plugin_stripe.KEY_NAME),
                   "mode": plugin_stripe.mode()},
        "usps": {"set": plugin_usps.configured(), "masked": secrets_env.mask(plugin_usps.SECRET_NAME),
                 "client_id": secrets_env.mask(plugin_usps.ID_NAME),
                 "origin_zip": secrets_env.get(plugin_usps.ZIP_NAME)},
    }


def save_integrations(data):
    """Write submitted keys to .env. Blank fields keep what is already saved."""
    saved = []
    stripe_key = str(data.get("stripe_key", "")).strip()
    if stripe_key:
        plugin_stripe.save_key(stripe_key)
        saved.append("Stripe key")
    cid = str(data.get("usps_client_id", "")).strip()
    csecret = str(data.get("usps_client_secret", "")).strip()
    zip_code = str(data.get("usps_origin_zip", "")).strip()
    if cid or csecret or zip_code:
        cid = cid or secrets_env.get(plugin_usps.ID_NAME)
        csecret = csecret or secrets_env.get(plugin_usps.SECRET_NAME)
        zip_code = zip_code or secrets_env.get(plugin_usps.ZIP_NAME)
        if not (cid and csecret and zip_code):
            raise ValueError("USPS needs the Client ID, the Client secret and the ZIP code you ship from.")
        plugin_usps.save_credentials(cid, csecret, zip_code)
        saved.append("USPS details")
    if not saved:
        raise ValueError("Nothing to save: paste a key first.")
    return saved


def run_polish_job(job, folder, feedback, undo=False, lint_only=False):
    ROUTER.local.job = job
    try:
        app = ProjectOrchestrator()
        project = _find_project(app, folder)
        polisher = CSSPolisher(project, store=app.store)
        if undo:
            job.log(f"[polish] {polisher.undo()}")
        elif lint_only:
            job.log(f"[polish] Running the free automatic fixes for {project['name']} (no AI) ...")
            job.log(f"[polish] {polisher.polish(auto_lint_only=True)}")
        else:
            job.log(f"[polish] Polishing styles for {project['name']} ...")
            polisher.polish(feedback=feedback)
        _finish(job, folder)
        job.log("Done.")
    except Exception as exc:
        job.error = str(exc)
        job.pending = "failed"
        job.log(f"ERROR: {exc}")
    finally:
        ROUTER.local.job = None
        BUILD_LOCK.release()
        job.state = job.pending


def run_mobile_job(job, folder):
    """Use the CSS agent to make an existing site adapt to phones and tablets."""
    ROUTER.local.job = job
    try:
        app = ProjectOrchestrator()
        project = _find_project(app, folder)
        polisher = CSSPolisher(project, store=app.store)
        job.log(f"[mobile] Inspecting the styles for {project['name']} ...")
        feedback = (
            "Make this existing website fully responsive and mobile-friendly. "
            "Inspect the site's existing HTML pages and CSS before changing styles. "
            "Prioritize phone widths of 360px, 390px, and 430px, then tablets around "
            "768px, while preserving the current desktop design. Fix actual causes "
            "of narrow-screen layout problems rather than hiding overflow. Remove "
            "or override fixed pixel widths, fixed minimum widths, large fixed "
            "margins, and desktop-only multi-column layouts when they make content "
            "too wide. Use fluid widths, max-width:100%, min-width:0, flex-wrap, and "
            "responsive CSS Grid/Flexbox as appropriate. Make images, pictures, "
            "videos, and embedded media scale within their containers without "
            "distortion. Make navigation, hero sections, text columns, cards, product "
            "grids, forms, buttons, tables, and footer content fit and stack naturally "
            "on small screens. Ensure readable typography, comfortable touch targets, "
            "sensible spacing, and no horizontal page scrolling. Use mobile/tablet "
            "media queries where needed and keep desktop styling intact above those "
            "breakpoints. Do not change wording, branding, colors, product data, links, "
            "HTML structure, JavaScript, cart behavior, checkout, Stripe, USPS, or "
            "other functionality. Avoid generic rules that break desktop styling. "
            "If a page lacks the standard viewport meta tag, report that clearly "
            "because CSS-only edits cannot add it. Save responsive CSS using the "
            "existing CSS polisher workflow."
        )
        job.log("[mobile] Applying responsive layout improvements (desktop styling preserved) ...")
        result = polisher.polish(feedback=feedback)
        job.log(f"[mobile] {result}")
        missing_viewport = []
        for page in sorted(Path(project["root_path"]).glob("*.html")):
            html = page.read_text(encoding="utf-8", errors="replace")
            if not re.search(r"<meta\s+[^>]*name\s*=\s*[\"']viewport[\"']", html, re.I):
                missing_viewport.append(page.name)
        if missing_viewport:
            job.log("[mobile] WARNING: viewport meta tag missing from "
                    + ", ".join(missing_viewport[:8])
                    + ". CSS breakpoints may not activate correctly on phones.")
        else:
            job.log("[mobile] Viewport meta tag found on all HTML pages.")
        job.log("[mobile] Responsive styling pass finished. Preview the site on a phone before publishing.")
        _finish(job, folder)
        job.log("Done.")
    except Exception as exc:
        job.error = str(exc)
        job.pending = "failed"
        job.log(f"ERROR: {exc}")
    finally:
        ROUTER.local.job = None
        BUILD_LOCK.release()
        job.state = job.pending


def list_sites():
    try:
        known = {Path(p["root_path"]).name: p for p in ProjectOrchestrator().store.list_projects()}
    except Exception:
        known = {}
    sites = []
    for folder in sorted(PROJECTS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if (folder / "index.html").is_file():
            project = known.get(folder.name)
            label = f"{project['name']} (build {project['id']})" if project else folder.name
            sites.append({
                "name": label,
                "folder": folder.name,
                "url": f"/sites/{folder.name}/index.html",
                "has_polish_backup": (folder / "polish_history").is_dir()
                                  and any((folder / "polish_history").glob("styles.*.css")),
            })
    return sites


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Groq Website Studio</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--muted:#5d6572;--line:#dfe3ea;--brand:#4f46e5;--ok:#15803d;--bad:#b91c1c;--soft:#eef2ff}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b22;--ink:#eceef2;--muted:#9aa3b2;--line:#2a2f3a;--brand:#818cf8;--ok:#4ade80;--bad:#f87171;--soft:#1e2235}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,Segoe UI,Arial,sans-serif}
main{max-width:820px;margin:0 auto;padding:2rem 1rem 4rem}
h1{margin:.2rem 0 .2rem;font-size:1.8rem}.sub{color:var(--muted);margin:0 0 1.5rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:1.25rem;margin-bottom:1.25rem}
label{display:block;font-weight:600;margin:.9rem 0 .3rem}label:first-child{margin-top:0}
input,textarea{width:100%;padding:.7rem .8rem;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink);font:inherit}
textarea{min-height:130px;resize:vertical}
button,.btn{display:inline-block;margin-top:1rem;padding:.8rem 1.4rem;border:0;border-radius:10px;background:var(--brand);color:#fff;font:inherit;font-weight:700;cursor:pointer;text-decoration:none}
button:disabled{opacity:.5;cursor:not-allowed}
.btn-alt{background:transparent;color:var(--ink);border:1px solid var(--line);font-weight:600}
.status{display:flex;align-items:center;gap:.6rem;font-weight:600}
.dot{width:.7rem;height:.7rem;border-radius:50%;background:var(--brand);animation:pulse 1.2s infinite}
.dot.done{background:var(--ok);animation:none}.dot.failed{background:var(--bad);animation:none}
@keyframes pulse{50%{opacity:.25}}
pre{margin:.9rem 0 0;max-height:320px;overflow:auto;padding:.9rem;background:var(--bg);border:1px solid var(--line);border-radius:10px;font:13px/1.45 ui-monospace,Consolas,monospace;white-space:pre-wrap}
.hint{color:var(--muted);font-size:.9rem;margin:.4rem 0 0}.hidden{display:none}
ul.sites{list-style:none;margin:0;padding:0}
ul.sites li{padding:.55rem 0;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.5rem}
.acts{display:flex;flex-wrap:wrap;gap:.4rem}
a{color:var(--brand);font-weight:500}.err{color:var(--bad);font-weight:600}
.check{display:flex;gap:.5rem;align-items:center;font-weight:400;margin-top:1rem}.check input{width:auto}
.mini{margin:0;padding:.3rem .75rem;font-size:.85rem;border-radius:8px}
.tips{background:var(--soft);border-radius:10px;padding:.75rem .9rem;font-size:.88rem;margin:.5rem 0 .8rem;line-height:1.45}
.tips ul{margin:.25rem 0 0;padding-left:1.2rem}
details.more{margin-top:1rem;border-top:1px solid var(--line);padding-top:.8rem}
details summary{cursor:pointer;font-weight:600}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 1rem}
@media (max-width:560px){.grid2{grid-template-columns:1fr}}
.okmsg{color:var(--ok);font-weight:600}
.item-row{display:grid;grid-template-columns:110px 1fr auto;gap:.5rem 1rem;padding:1rem 0;border-top:1px solid var(--line);align-items:start}
.item-pic{display:flex;flex-direction:column;gap:.4rem;align-items:flex-start}
.item-thumb,.item-nopic{width:100px;height:100px;border:1px solid var(--line);border-radius:10px;object-fit:cover;background:var(--bg)}
.item-nopic{display:flex;align-items:center;justify-content:center;color:var(--muted,#666);font-size:.8rem;text-align:center}
.item-fields{display:grid;grid-template-columns:1fr 9rem;gap:0 1rem}
.item-fields .wide{grid-column:1 / -1}
.item-small{display:grid;grid-template-columns:1fr auto;gap:0 1rem;align-items:end;grid-column:1 / -1}
.item-err{grid-column:1 / -1;min-height:0}
.item-actions{display:flex;gap:.75rem;flex-wrap:wrap;margin-top:1rem}
.picbtn{cursor:pointer;display:inline-block}
.item-row [hidden]{display:none!important}
@media (max-width:620px){.item-row{grid-template-columns:1fr}.item-fields{grid-template-columns:1fr}}
.pe-block{border:1px solid var(--line);border-radius:12px;padding:.8rem 1rem;margin:.8rem 0;background:var(--bg)}
.pe-block.pe-shared{background:var(--soft)}
.pe-head{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;justify-content:space-between}
.pe-head h3{margin:0;font-size:1.05rem}
.pe-field{margin:.6rem 0}.pe-field label{margin:0 0 .2rem;font-size:.85rem;color:var(--muted)}
.pe-field.pe-bad input,.pe-field.pe-bad textarea{border-color:var(--bad)}
.pe-field textarea{min-height:4.5rem}
.pe-row{display:flex;gap:.6rem;align-items:flex-end}.pe-row>div{flex:1}
.pe-thumb{width:90px;height:68px;object-fit:cover;border:1px solid var(--line);border-radius:8px;background:#fff}
.pe-tabs{display:flex;flex-wrap:wrap;gap:.4rem;margin:.6rem 0}
.pe-tabs .on{outline:2px solid var(--brand)}
.pe-add{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:.5rem 0}
.pe-add select,.pe-add input{width:auto;margin:0}
.bx{border:1px solid var(--line);border-radius:12px;padding:.8rem 1rem;margin:.9rem 0;background:var(--bg)}
.bx h3{margin:0 0 .4rem;font-size:1.05rem}
.blk{border:1px solid var(--line);border-radius:10px;padding:.6rem .8rem;margin:.6rem 0;background:#fff}
.blk .hd{display:flex;flex-wrap:wrap;gap:.4rem;justify-content:space-between;align-items:center;font-weight:600}
.subs{width:100%;border-collapse:collapse;font-size:.9rem}.subs td,.subs th{padding:.3rem .4rem;border-bottom:1px solid var(--line);text-align:left}
.pill{display:inline-block;padding:0 .5rem;border-radius:999px;border:1px solid var(--line);font-size:.8rem}
</style></head><body><main>
<h1>Groq Website Studio</h1>
<p class="sub">Describe a website, press Build, and the finished page opens when it is ready.</p>

<div class="card">
  <label for="name">Website name (optional)</label>
  <input id="name" placeholder="e.g. Edwin's Page">
  <label for="brief">Describe the website you want</label>
  <textarea id="brief" placeholder="e.g. Homepage for a neighborhood bakery: sourdough, pastries, custom cakes, warm colors, order-inquiry form. Leave hours and phone as placeholders."></textarea>
  <p class="hint">Include the type of site, sections, tone and colors. Anything you do not specify becomes a [placeholder].</p>
  <label class="check"><input type="checkbox" id="expand" checked> Also create the pages the links point to (takes longer)</label>
  <label class="check"><input type="checkbox" id="add-contacts"> Add contacts (email and social media links in the footer of every page)</label>
  <div class="more hidden" id="contacts-fields">
    <p class="hint">Added to every page by the program itself, exactly as typed. For social media, type a handle (such as @mybakery) or paste the full link. Leave a box empty to skip it. Also used by "Add contacts" on a site in the list below.</p>
    <label for="x-email">Contact email</label>
    <input id="x-email" type="email" placeholder="you@yourdomain.com" autocomplete="off">
    <div class="grid2">
      <div><label for="s-facebook">Facebook</label><input id="s-facebook" placeholder="handle or link" autocomplete="off"></div>
      <div><label for="s-instagram">Instagram</label><input id="s-instagram" placeholder="handle or link" autocomplete="off"></div>
      <div><label for="s-x">X (Twitter)</label><input id="s-x" placeholder="handle or link" autocomplete="off"></div>
      <div><label for="s-tiktok">TikTok</label><input id="s-tiktok" placeholder="handle or link" autocomplete="off"></div>
      <div><label for="s-youtube">YouTube</label><input id="s-youtube" placeholder="handle or link" autocomplete="off"></div>
      <div><label for="s-linkedin">LinkedIn</label><input id="s-linkedin" placeholder="handle or link" autocomplete="off"></div>
    </div>
  </div>
  <label class="check"><input type="checkbox" id="shop"> Add shopping cart (Shop and Cart pages; items and prices are kept in items.csv in the site's folder)</label>
  <button id="go">Build website</button>
</div>

<details class="card" id="integrations">
  <summary>Shared payment keys (used by sites with no keys of their own) <span class="hint" id="int-summary"></span></summary>
  <p class="hint">Keys are saved to the .env file in the Studio folder on this computer. They are never written into a web page and are never shown again here (only the last 4 characters). Leave a box empty to keep what is saved.</p>
  <label for="k-stripe">Stripe secret key (starts with sk_test_ or sk_live_)</label>
  <input id="k-stripe" type="password" autocomplete="new-password" placeholder="sk_test_...">
  <label for="k-usps-id">USPS Client ID (Consumer Key)</label>
  <input id="k-usps-id" type="password" autocomplete="new-password">
  <label for="k-usps-secret">USPS Client secret (Consumer Secret)</label>
  <input id="k-usps-secret" type="password" autocomplete="new-password">
  <label for="k-usps-zip">ZIP code you ship from</label>
  <input id="k-usps-zip" inputmode="numeric" maxlength="10" autocomplete="off">
  <button id="int-save" type="button">Save keys</button>
  <p class="hint" id="int-status"></p>
</details>

<div class="card hidden" id="pk-stripe-panel" style="border-color:var(--brand)">
  <h2>Payments (Stripe): <span class="pk-target"></span></h2>
  <p class="hint">Lets shoppers pay by card at checkout. Saved in a hidden file in this site's own folder, never written into a web page, and visitors cannot download it. Only the last 4 characters are shown again. Leave a box empty to keep what is saved.</p>
  <p class="hint err">Keep the site folder private: do not upload it to a plain web host that serves hidden files. Run the shop with the Studio or shop_server.py.</p>
  <p class="hint pk-state"></p>
  <label>Stripe secret key (starts with sk_test_ or sk_live_)</label>
  <input class="pk-f" data-k="stripe_key" type="password" autocomplete="new-password" placeholder="sk_test_...">
  <div class="item-actions">
    <button class="pk-save" type="button">Save payment key</button>
    <button class="btn-alt pk-import" type="button">Copy the shared keys into this site</button>
    <button class="btn-alt pk-clear" type="button">Remove this site's keys</button>
    <button class="btn-alt pk-close" type="button">Close</button>
  </div>
  <p class="hint pk-status"></p>
</div>
<div class="card hidden" id="pk-usps-panel" style="border-color:var(--brand)">
  <h2>Shipping (USPS): <span class="pk-target"></span></h2>
  <p class="hint">Lets shoppers see USPS shipping prices and choose a service at checkout. Saved in a hidden file in this site's own folder, never written into a web page, and visitors cannot download it. Only the last 4 characters are shown again. Leave a box empty to keep what is saved.</p>
  <p class="hint err">Keep the site folder private: do not upload it to a plain web host that serves hidden files. Run the shop with the Studio or shop_server.py.</p>
  <p class="hint pk-state"></p>
  <label>USPS Client ID (Consumer Key)</label>
  <input class="pk-f" data-k="usps_client_id" type="password" autocomplete="new-password">
  <label>USPS Client secret (Consumer Secret)</label>
  <input class="pk-f" data-k="usps_client_secret" type="password" autocomplete="new-password">
  <label>ZIP code you ship from</label>
  <input class="pk-f pk-zip" data-k="usps_origin_zip" inputmode="numeric" maxlength="10" autocomplete="off">
  <div class="item-actions">
    <button class="pk-save" type="button">Save shipping details</button>
    <button class="btn-alt pk-import" type="button">Copy the shared keys into this site</button>
    <button class="btn-alt pk-clear" type="button">Remove this site's keys</button>
    <button class="btn-alt pk-close" type="button">Close</button>
  </div>
  <p class="hint pk-status"></p>
</div>
<div class="card hidden" id="nw-panel" style="border-color:var(--brand)">
  <h2>Newsletter: <span id="nw-target"></span></h2>
  <p class="hint" id="nw-status"></p>
  <div class="bx"><h3>1. Sign-up form on the website</h3>
    <p class="hint" id="nw-installed"></p>
    <button id="nw-install" type="button">Add the sign-up form to the website</button></div>
  <div class="bx"><h3>2. Email account (how newsletters are sent)</h3>
    <p class="hint">Any provider that offers SMTP works. Gmail is fine to start (about 500 emails a day, and it needs an app password); Brevo, Mailgun, Amazon SES, Resend or SendGrid are better for a bigger list. The password is kept in the site's hidden key file and never shown again.</p>
    <div class="grid2">
      <div><label>Provider<select id="nw-preset"></select></label></div>
      <div><label>Mail server<input id="nw-host" autocomplete="off"></label></div>
      <div><label>Port<input id="nw-port" inputmode="numeric" autocomplete="off"></label></div>
      <div><label>Connection<select id="nw-sec"><option value="ssl">SSL</option><option value="starttls">STARTTLS</option></select></label></div>
      <div><label>User name<input id="nw-user" autocomplete="off"></label></div>
      <div><label>Password or app password<input id="nw-pass" type="password" autocomplete="new-password"></label></div>
      <div><label>Name emails come from<input id="nw-fromname" maxlength="80" autocomplete="off"></label></div>
      <div><label>Email address emails come from<input id="nw-fromemail" autocomplete="off"></label></div>
      <div><label>Replies go to (optional)<input id="nw-reply" autocomplete="off"></label></div>
      <div><label>Your website's public address<input id="nw-url" placeholder="https://www.example.com" autocomplete="off"></label></div>
      <div><label>Emails per day limit<input id="nw-limit" inputmode="numeric" autocomplete="off"></label></div>
      <div><label>Accent colour<input id="nw-accent" type="color"></label></div>
    </div>
    <label>Postal address (the law requires it at the bottom of every newsletter)<textarea id="nw-postal" maxlength="300"></textarea></label>
    <label class="check"><input type="checkbox" id="nw-double"> Ask new subscribers to confirm by email first (recommended)</label>
    <div class="item-actions"><button id="nw-save" type="button">Save email settings</button><button class="btn-alt" id="nw-test" type="button">Test the connection</button></div>
    <p class="hint" id="nw-set-status"></p></div>
  <div class="bx"><h3>3. Subscribers</h3>
    <p class="hint" id="nw-counts"></p>
    <div id="nw-subs"></div>
    <div class="item-actions"><a class="btn-alt picbtn mini" id="nw-export" href="#">Download the list (CSV)</a>
      <button class="btn-alt mini hidden" id="nw-sync" type="button">Sync with the published website</button>
      <button class="btn-alt mini" id="nw-confirm" type="button">Send confirmation emails to people still waiting</button></div>
    <details class="more"><summary>Add people you already have permission to email</summary>
      <textarea id="nw-import" placeholder="Paste email addresses, separated by spaces, commas or lines"></textarea>
      <label class="check"><input type="checkbox" id="nw-consent"> These people agreed to receive emails from this website</label>
      <button class="btn-alt mini" id="nw-import-go" type="button">Add them</button></details>
    <p class="hint" id="nw-sub-status"></p></div>
  <div class="bx"><h3>4. Newsletters</h3>
    <div id="nw-camps"></div>
    <button class="btn-alt" id="nw-new" type="button">Write a new newsletter</button>
    <div id="nw-editor" class="hidden">
      <label>Subject line<input id="nw-subject" maxlength="200" autocomplete="off"></label>
      <div id="nw-blocks"></div>
      <div class="item-actions"><button id="nw-draft" type="button">Save draft</button>
        <input id="nw-testto" placeholder="Send a test to this email" style="width:16rem" autocomplete="off">
        <button class="btn-alt" id="nw-sendtest" type="button">Send test</button>
        <button id="nw-send" type="button">Send to all subscribers</button>
        <button class="btn-alt" id="nw-cancel" type="button">Close editor</button></div>
    </div>
    <p class="hint" id="nw-send-status"></p></div>
  <div class="item-actions"><button class="btn-alt" id="nw-close" type="button">Close</button></div>
</div>
<div class="card hidden" id="bg-panel" style="border-color:var(--brand)">
  <h2>Blog: <span id="bg-target"></span></h2>
  <p class="hint">The blog is its own part of the website: a Blog page with the list of posts, a page for each post, and a feed. Drafts never appear on the site.</p>
  <div class="bx"><h3>Blog on the website</h3><p class="hint" id="bg-installed"></p>
    <button id="bg-install" type="button">Add the Blog to the website (page and menu link)</button></div>
  <div class="bx"><h3>Posts</h3><div id="bg-posts"></div>
    <button class="btn-alt" id="bg-new" type="button">Write a new post</button></div>
  <div class="bx hidden" id="bg-editor"><h3>Post</h3>
    <div class="grid2"><div><label>Title<input id="bg-title" maxlength="150" autocomplete="off"></label></div>
      <div><label>Date<input id="bg-date" type="date"></label></div>
      <div><label>Status<select id="bg-status"><option value="draft">Draft (not on the website)</option><option value="published">Published</option></select></label></div>
      <div><label>Cover picture (optional)<input id="bg-cover" placeholder="assets/site/picture.jpg" autocomplete="off"></label>
        <label class="mini btn-alt picbtn">Upload cover<input type="file" accept="image/*" id="bg-cover-file" hidden></label></div></div>
    <label>Short summary shown in the list (optional)<input id="bg-excerpt" maxlength="300" autocomplete="off"></label>
    <div id="bg-blocks"></div>
    <div class="item-actions"><button id="bg-save" type="button">Save post</button><button class="btn-alt" id="bg-cancel" type="button">Close editor</button></div></div>
  <p class="hint" id="bg-status-msg"></p>
  <div class="item-actions"><button class="btn-alt" id="bg-close" type="button">Close</button></div>
</div>
<div class="card hidden" id="cf-panel" style="border-color:var(--brand)">
  <h2>Publish to Cloudflare: <span id="cf-target"></span></h2>
  <div class="bx"><h3>One-time setup (about 5 minutes)</h3>
    <ol class="hint">
      <li>Sign in at dash.cloudflare.com. Your <b>Account ID</b> is on the right side of the home page (or in the address bar).</li>
      <li>Open My Profile, then API Tokens, then Create Token, then Create Custom Token. Give it two permissions: <b>Account: Cloudflare Pages: Edit</b> and <b>Account: D1: Edit</b> (D1 stores newsletter sign-ups), choose your account, and create it. Copy the token (it is shown once).</li>
      <li>Install Node.js (the LTS version from nodejs.org) on this computer if it is not installed. <span id="cf-node"></span></li>
      <li>Paste the Account ID and token below, choose a project name, and press Save, then Check connection.</li></ol>
    <div class="grid2">
      <div><label>Account ID<input id="cf-account" autocomplete="off"></label></div>
      <div><label>Project name (becomes name.pages.dev)<input id="cf-project" autocomplete="off"></label></div>
      <div><label>API token<input id="cf-token" type="password" autocomplete="new-password"></label></div>
      <div><label>Your own domain (optional)<input id="cf-domain" placeholder="www.example.com" autocomplete="off"></label></div></div>
    <div class="item-actions"><button id="cf-save" type="button">Save</button><button class="btn-alt" id="cf-check" type="button">Check connection</button></div></div>
  <div class="bx"><h3>Publish</h3>
    <p class="hint" id="cf-last"></p><p class="hint err" id="cf-warn"></p>
    <div class="item-actions"><button id="cf-deploy" type="button">Publish now</button>
      <button class="btn-alt" id="cf-domain-go" type="button">Connect my domain</button></div>
    <p class="hint">Only public files are uploaded: keys, the subscriber list, your item spreadsheet and backups never leave this computer.</p></div>
  <p class="hint" id="cf-status"></p>
  <div class="item-actions"><button class="btn-alt" id="cf-close" type="button">Close</button></div>
</div>
<div class="card hidden" id="pe-panel" style="border-color:var(--brand)">
  <h2>Edit pages: <span id="pe-target"></span></h2>
  <p class="hint">Change the text in the boxes and press Save. Nothing here uses AI, and your previous version is kept so you can press Undo.</p>
  <div class="pe-tabs" id="pe-tabs"></div>
  <div class="pe-add">
    <input id="pe-newpage" placeholder="New page name, e.g. Care Guides" maxlength="60">
    <label class="check" style="margin:0"><input type="checkbox" id="pe-inmenu" checked> Add to menu</label>
    <button class="btn-alt mini" id="pe-addpage" type="button">Add page</button>
  </div>
  <p class="hint" id="pe-status"></p>
  <div id="pe-body"></div>
  <div class="item-actions">
    <button id="pe-save" type="button">Save changes</button>
    <button class="btn-alt" id="pe-undo" type="button">Undo last change</button>
    <button class="btn-alt" id="pe-view" type="button">View page</button>
    <button class="btn-alt" id="pe-delpage" type="button">Delete this page</button>
    <button class="btn-alt" id="pe-close" type="button">Close</button>
  </div>
</div>
<datalist id="shop-sections-list"></datalist>
<div class="card hidden" id="items-panel" style="border-color:var(--brand)">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <strong>Shop items: <span id="items-target"></span></strong>
    <button class="mini btn-alt" id="items-close" type="button">Close</button>
  </div>
  <p class="hint">Fill in one set of boxes for each item. Only the name and price are required. A picture you choose is shrunk to a web-friendly size and saved inside the site's folder.</p>
  <div id="items-list"></div>
  <div class="item-actions">
    <button class="btn-alt" id="items-add" type="button">+ Add an item</button>
    <button id="items-save" type="button">Save and update the shop</button>
  </div>
  <p class="hint" id="items-status"></p>
  <p class="hint" id="items-path"></p>
</div>

<div class="card hidden" id="polish-panel" style="border-color:var(--brand)">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <strong>Polish CSS: <span id="polish-target"></span></strong>
    <button class="mini btn-alt" id="polish-close" type="button">Close</button>
  </div>
  <div class="tips">
    <strong>Examples of things you can ask the CSS polisher to fix:</strong>
    <ul>
      <li><b>Spacing:</b> "Add more padding between sections" or "Increase gaps between cards and list items"</li>
      <li><b>Layout:</b> "Stack items vertically instead of side-by-side" or "Center the hero text"</li>
      <li><b>Visual separation:</b> "Add subtle borders or alternating backgrounds between sections"</li>
      <li><b>Colors &amp; Fonts:</b> "Make the background darker with brighter yellow accents" or "Make headings larger"</li>
      <li><b>Free fixes only:</b> repairs known CSS bugs (missing units, hidden-text classes) without using any AI quota</li>
      <li><b>Auto-Diagnose:</b> Click <b>Auto-Diagnose Issues</b> to have an AI agent inspect your page and fill in recommended fixes automatically</li>
    </ul>
  </div>
  <label for="polish-feedback">What looks wrong or needs improvement? (optional)</label>
  <textarea id="polish-feedback" style="min-height:95px" placeholder="Click 'Auto-Diagnose Issues' to inspect the page automatically, or type your own instructions here..."></textarea>
  <div style="display:flex;gap:.5rem;flex-wrap:wrap">
    <button id="polish-auto" class="btn-alt" type="button">Auto-Diagnose Issues</button>
    <button id="polish-free" class="btn-alt" type="button">Free fixes only (no AI)</button>
    <button id="polish-run" type="button">Apply CSS Polish</button>
    <button id="polish-undo" class="btn-alt hidden" type="button">Undo last polish</button>
  </div>
</div>

<div class="card hidden" id="progress">
  <div class="status"><span class="dot" id="dot"></span><span id="phase">Starting...</span></div>
  <pre id="log"></pre>
  <div id="result" class="hidden"><a class="btn" id="open" target="_blank" rel="noopener">Open your website</a></div>
</div>

<div class="card"><strong>Previous sites</strong><ul class="sites" id="sites"></ul></div>
</main>
<script>
const $=id=>document.getElementById(id);
let timer=null,job=null,since=0,polishFolder=null,sitesCache=[];
function setBusy(b){["go","polish-run","polish-undo","polish-auto","polish-free","int-save"].forEach(id=>{$(id).disabled=b;});}
const SOCIALS=["facebook","instagram","x","tiktok","youtube","linkedin"];
function getSettings(){
  const social={};SOCIALS.forEach(k=>{social[k]=$("s-"+k).value.trim();});
  return {contacts:$("add-contacts").checked,email:$("x-email").value.trim(),social,shop:{enabled:$("shop").checked}};
}
function showContacts(){$("contacts-fields").classList.toggle("hidden",!$("add-contacts").checked);}
function contactsFilled(){return !!($("x-email").value.trim()||SOCIALS.some(k=>$("s-"+k).value.trim()));}
async function loadIntegrations(){
  try{
    const d=await (await fetch("/api/integrations")).json();
    const parts=[];
    parts.push("Stripe: "+(d.stripe.set?"saved ("+d.stripe.masked+(d.stripe.mode?", "+d.stripe.mode+" mode":"")+")":"not set"));
    parts.push("USPS: "+(d.usps.set?"saved (secret "+d.usps.masked+")":"not set"));
    $("int-summary").textContent="- "+parts.join("; ");
    if(d.usps.origin_zip)$("k-usps-zip").placeholder="saved: "+d.usps.origin_zip;
  }catch(e){}
}
function refreshUndo(){const m=sitesCache.find(x=>x.folder===polishFolder);$("polish-undo").classList.toggle("hidden",!(m&&m.has_polish_backup));}

function phaseFrom(line){
  if(/\[mobile\]/.test(line))return"Making the website mobile-friendly";
  if(/\[polish\]/.test(line))return"Polishing CSS styles";
  if(/\[pages\]/.test(line))return"Creating the linked pages";
  if(/\[extras\]|\[shop\]|\[contacts\]/.test(line))return"Adding contacts and shop";
  if(/\[images\]/.test(line))return"Sourcing images";
  if(/waiting \d+s/.test(line))return"Waiting for the free-tier rate limit (normal)";
  if(/HTML /.test(line))return"Writing page sections";
  if(/CSS /.test(line))return"Styling the page";
  if(/Created project/.test(line))return"Planning the site";
  return null;
}

function openPolish(s){
  polishFolder=s.folder;
  $("polish-target").textContent=s.name;
  $("polish-feedback").value="";
  $("polish-undo").classList.toggle("hidden",!s.has_polish_backup);
  $("polish-panel").classList.remove("hidden");
  $("polish-panel").scrollIntoView({behavior:"smooth",block:"nearest"});
}

async function loadSites(){
  const r=await fetch("/api/sites");
  const list=await r.json();
  sitesCache=list;refreshUndo();
  const ul=$("sites");
  ul.innerHTML="";
  if(!list.length){ul.innerHTML="<li>None yet.</li>";return;}
  list.forEach(s=>{
    const li=document.createElement("li");
    const a=document.createElement("a");
    a.href=s.url;a.target="_blank";a.rel="noopener";a.textContent=s.name;
    li.appendChild(a);
    const acts=document.createElement("div");
    acts.className="acts";
    const bp=document.createElement("button");
    bp.className="mini";bp.textContent="Polish CSS";bp.onclick=()=>openPolish(s);
    acts.appendChild(bp);
    const bm=document.createElement("button");
    bm.className="mini";bm.textContent="Make Mobile-Friendly";
    bm.title="Improve phone and tablet layouts while preserving desktop styling";
    bm.onclick=()=>{
      if(confirm("Improve the mobile layout for "+s.name+"? The CSS polisher creates a backup that you can undo in Polish CSS.")){
        begin("/api/mobile",{folder:s.folder});
      }
    };
    acts.appendChild(bm);
    const be=document.createElement("button");
    be.className="mini btn-alt";be.textContent="Add linked pages";be.onclick=()=>begin("/api/expand",{folder:s.folder});
    acts.appendChild(be);
    const mk=(label,title,fn)=>{const b=document.createElement("button");b.className="mini btn-alt";b.textContent=label;b.title=title;b.onclick=fn;acts.appendChild(b);};
    mk("Add contacts","Add the email and social links typed in the form at the top to this site",()=>{
      if(!contactsFilled()){$("add-contacts").checked=true;showContacts();window.scrollTo({top:0,behavior:"smooth"});$("x-email").focus();alert("Type the email and/or social media handles in the form at the top, then press Add contacts again.");return;}
      begin("/api/extras",{folder:s.folder,action:"contacts",settings:getSettings()});});
    mk("Newsletter","Sign-ups, subscriber list, and writing and sending newsletters",()=>openNews(s));
    mk("Blog","Write and publish blog posts on their own Blog page",()=>openBlog(s));
    mk("Publish to Cloudflare","Put this site online with Cloudflare Pages",()=>openCf(s));
    mk("Payments","Add this site's own Stripe key so shoppers can pay",()=>openKeys(s,"stripe"));
    mk("Shipping","Add this site's own USPS details for shipping prices",()=>openKeys(s,"usps"));
    mk("Edit pages","Edit the text, pictures and links of every page, and add pages or sections",()=>openPages(s));
    mk("Edit shop items","Type the items and prices for this site's shop",()=>openItems(s));
    mk("Add shopping cart","Add the shop to this site, or refresh it after editing items.csv",()=>begin("/api/extras",{folder:s.folder,action:"shop"}));
    mk("Remove contacts","Remove the email and social links that were added to this site",()=>{if(confirm("Remove the contact email and social links from "+s.name+"?"))begin("/api/extras",{folder:s.folder,action:"remove-contacts"});});
    mk("Remove shop","Remove the shopping cart pages from this site (items.csv is kept)",()=>{if(confirm("Remove the shopping cart from "+s.name+"? Your items.csv file is kept."))begin("/api/extras",{folder:s.folder,action:"remove-shop"});});
    li.appendChild(acts);
    ul.appendChild(li);
  });
}

async function poll(){
  const r=await fetch(`/api/status?job=${job}&since=${since}`);
  const d=await r.json();
  d.lines.forEach(l=>{$("log").textContent+=l+"\n";const p=phaseFrom(l);if(p)$("phase").textContent=p;});
  since=d.next;$("log").scrollTop=$("log").scrollHeight;
  if(d.state==="done"){
    clearInterval(timer);$("dot").className="dot done";$("phase").textContent="Done - your website is ready";
    $("open").href=d.site_url;$("result").classList.remove("hidden");
    setBusy(false);
    loadSites();window.open(d.site_url,"_blank");
  }else if(d.state==="failed"){
    clearInterval(timer);$("dot").className="dot failed";
    $("phase").innerHTML='<span class="err">Task failed - see the log below</span>';
    setBusy(false);
  }
}

async function begin(url,payload){
  setBusy(true);
  $("progress").classList.remove("hidden");$("result").classList.add("hidden");
  $("log").textContent="";$("dot").className="dot";$("phase").textContent="Starting...";since=0;
  const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
  const d=await r.json();
  if(!r.ok){
    $("dot").className="dot failed";$("phase").innerHTML='<span class="err"></span>';
    $("phase").firstChild.textContent=d.error||"Could not start";
    setBusy(false);return;
  }
  job=d.job;timer=setInterval(poll,1000);
}

$("go").onclick=()=>{
  const brief=$("brief").value.trim();
  if(brief.length<10){alert("Please describe the website you want (at least a sentence).");return;}
  begin("/api/build",{name:$("name").value.trim(),brief,expand:$("expand").checked,settings:getSettings()});
};
$("int-save").onclick=async()=>{
  const body={stripe_key:$("k-stripe").value.trim(),usps_client_id:$("k-usps-id").value.trim(),
    usps_client_secret:$("k-usps-secret").value.trim(),usps_origin_zip:$("k-usps-zip").value.trim()};
  const out=$("int-status");out.className="hint";out.textContent="Saving...";
  try{
    const r=await fetch("/api/integrations",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const d=await r.json();
    if(!r.ok){out.className="hint err";out.textContent=d.error||"Could not save.";return;}
    ["k-stripe","k-usps-id","k-usps-secret","k-usps-zip"].forEach(id=>{$(id).value="";});
    out.className="hint okmsg";out.textContent="Saved: "+d.saved.join(", ")+". The keys are stored in .env and cleared from this form.";
    loadIntegrations();
  }catch(e){out.className="hint err";out.textContent="Could not reach the Studio.";}
};
let itemsFolder="";
function itemRow(it){
  it=it||{};
  const row=document.createElement("div");row.className="item-row";row.dataset.id=it.id||"";row.dataset.image=it.image||"";
  row.innerHTML='<div class="item-pic"><img class="item-thumb" alt="" hidden><div class="item-nopic">No picture</div>'
    +'<label class="mini btn-alt picbtn">Choose picture<input type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden></label>'
    +'<button class="mini btn-alt rmpic" type="button" hidden>Remove picture</button></div>'
    +'<div class="item-fields">'
    +'<div><label>Item name<input class="f-name" maxlength="120" autocomplete="off"></label></div>'
    +'<div><label>Item price<input class="f-price" inputmode="decimal" placeholder="24.00" autocomplete="off"></label></div>'
    +'<div class="wide"><label>Section (optional; each section gets its own page, with the Shop page listing them)<input class="f-section" list="shop-sections-list" maxlength="60" autocomplete="off" placeholder="e.g. Plants, Merch"></label></div>'
    +'<div class="wide"><label>Description (optional)<input class="f-desc" maxlength="400" autocomplete="off"></label></div>'
    +'<div class="item-small"><label>Weight in ounces (optional, used for USPS prices)<input class="f-weight" inputmode="decimal" autocomplete="off"></label>'
    +'<label class="check"><input type="checkbox" class="f-stock"> In stock</label></div></div>'
    +'<button class="mini btn-alt rm" type="button">Remove item</button><div class="item-err err"></div>';
  row.querySelector(".f-name").value=it.name||"";row.querySelector(".f-price").value=it.price||"";
  row.querySelector(".f-desc").value=it.description||"";row.querySelector(".f-weight").value=it.weight_oz||"";
  row.querySelector(".f-stock").checked=it.in_stock!==false;row.querySelector(".f-section").value=it.section||"";
  showPic(row);
  row.querySelector(".rm").onclick=()=>row.remove();
  row.querySelector(".rmpic").onclick=()=>{row.dataset.image="";showPic(row);};
  row.querySelector('input[type=file]').onchange=e=>{const f=e.target.files[0];e.target.value="";if(f)uploadPic(row,f);};
  return row;
}
function showPic(row){
  const img=row.querySelector(".item-thumb"),none=row.querySelector(".item-nopic"),has=!!row.dataset.image;
  if(has)img.src="/sites/"+itemsFolder+"/"+row.dataset.image+"?v="+Date.now();
  img.hidden=!has;none.hidden=has;row.querySelector(".rmpic").hidden=!has;
}
function shrinkToJpeg(file){
  return createImageBitmap(file).then(bmp=>{
    const k=Math.min(1,1200/Math.max(bmp.width,bmp.height));
    const c=document.createElement("canvas");c.width=Math.max(1,Math.round(bmp.width*k));c.height=Math.max(1,Math.round(bmp.height*k));
    const x=c.getContext("2d");x.fillStyle="#fff";x.fillRect(0,0,c.width,c.height);x.drawImage(bmp,0,0,c.width,c.height);
    return new Promise((ok,no)=>c.toBlob(b=>b?ok(b):no(new Error("x")),"image/jpeg",0.86));
  });
}
function blobToBase64(blob){
  return new Promise((ok,no)=>{const r=new FileReader();r.onload=()=>ok(String(r.result).split(",")[1]);r.onerror=no;r.readAsDataURL(blob);});
}
async function uploadPic(row,file){
  const err=row.querySelector(".item-err");err.textContent="";
  if(file.size>30*1024*1024){err.textContent="That picture is too large (30 MB maximum).";return;}
  err.textContent="Uploading picture...";err.className="item-err hint";
  try{
    const blob=await shrinkToJpeg(file);
    const r=await fetch("/api/item-image",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({folder:itemsFolder,hint:row.querySelector(".f-name").value,data:await blobToBase64(blob)})});
    const d=await r.json();
    if(!r.ok){err.className="item-err err";err.textContent=d.error||"Could not save the picture.";return;}
    row.dataset.image=d.path;err.textContent="";showPic(row);
  }catch(e){err.className="item-err err";err.textContent="That file could not be read as a picture.";}
}
function refreshSections(){
  const names=[...new Set(Array.from(document.querySelectorAll("#items-list .f-section")).map(i=>i.value.trim()).filter(Boolean))];
  const dl=$("shop-sections-list");dl.textContent="";names.forEach(n=>{const o=document.createElement("option");o.value=n;dl.appendChild(o);});
}
document.addEventListener("focusin",e=>{if(e.target.classList&&e.target.classList.contains("f-section"))refreshSections();});
function collectItems(){
  return Array.from(document.querySelectorAll("#items-list .item-row")).map(r=>({
    id:r.dataset.id,name:r.querySelector(".f-name").value,price:r.querySelector(".f-price").value,
    description:r.querySelector(".f-desc").value,weight_oz:r.querySelector(".f-weight").value,
    image:r.dataset.image,in_stock:r.querySelector(".f-stock").checked,section:r.querySelector(".f-section").value}));
}
async function openItems(s){
  itemsFolder=s.folder;$("items-target").textContent=s.name;
  const st=$("items-status");st.className="hint";st.textContent="Loading...";
  $("items-list").textContent="";$("items-panel").classList.remove("hidden");window.scrollTo({top:0,behavior:"smooth"});
  try{
    const d=await (await fetch("/api/items?folder="+encodeURIComponent(s.folder))).json();
    if(d.error){st.className="hint err";st.textContent=d.error;return;}
    (d.items.length?d.items:[{}]).forEach(it=>$("items-list").appendChild(itemRow(it)));
    $("items-path").textContent="The list is kept in "+d.path+" (you can also edit that file by hand).";
    st.textContent=d.warnings.length?"Some rows in the file were skipped: "+d.warnings.join(" | "):"";
    if(d.warnings.length)st.className="hint err";
  }catch(e){st.className="hint err";st.textContent="Could not load the item list.";}
}
$("items-add").onclick=()=>{const r=itemRow({});$("items-list").appendChild(r);r.querySelector(".f-name").focus();};
$("items-close").onclick=()=>$("items-panel").classList.add("hidden");
$("items-save").onclick=async()=>{
  const st=$("items-status");st.className="hint";st.textContent="Saving...";
  document.querySelectorAll("#items-list .item-err").forEach(e=>{e.textContent="";e.className="item-err err";});
  try{
    const r=await fetch("/api/items",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({folder:itemsFolder,items:collectItems()})});
    const d=await r.json();
    if(!r.ok){
      st.className="hint err";st.textContent=d.error||"Could not save.";
      const rows=document.querySelectorAll("#items-list .item-row");
      Object.keys(d.rows||{}).forEach(i=>{if(rows[i])rows[i].querySelector(".item-err").textContent=d.rows[i];});
      return;
    }
    st.className="hint okmsg";st.textContent=d.items+" item(s) saved. Updating the shop...";
    begin("/api/extras",{folder:itemsFolder,action:"shop"});
  }catch(e){st.className="hint err";st.textContent="Could not reach the Studio.";}
};
$("add-contacts").onchange=showContacts;
$("polish-close").onclick=()=>$("polish-panel").classList.add("hidden");
$("polish-auto").onclick=async()=>{
  if(!polishFolder)return;
  const btn=$("polish-auto");btn.disabled=true;btn.textContent="Inspecting page...";
  try{
    const r=await fetch("/api/critique",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({folder:polishFolder})});
    const d=await r.json();
    if(!r.ok)alert(d.error||"Diagnosis failed");
    else $("polish-feedback").value=d.suggestion;
  }finally{btn.disabled=false;btn.textContent="Auto-Diagnose Issues";}
};
$("polish-run").onclick=()=>{if(polishFolder)begin("/api/polish",{folder:polishFolder,feedback:$("polish-feedback").value.trim(),undo:false});};
$("polish-free").onclick=()=>{if(polishFolder)begin("/api/polish",{folder:polishFolder,feedback:"",undo:false,lint_only:true});};
$("polish-undo").onclick=()=>{if(polishFolder)begin("/api/polish",{folder:polishFolder,feedback:"",undo:true});};

let peFolder="",peFile="index.html",peData=null,peBusy=false;
function peSay(t,cls){const st=$("pe-status");st.className="hint "+(cls||"");st.textContent=t||"";}
function peMk(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;}
async function peCall(url,payload){
  if(peBusy)return null;peBusy=true;
  try{
    const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(Object.assign({folder:peFolder},payload))});
    const d=await r.json();
    if(!r.ok){
      if(d.fields){peData&&peRender();Object.keys(d.fields).forEach(k=>{const el=document.querySelector('[data-fid="'+k+'"]');
        if(el){el.classList.add("pe-bad");const m=el.querySelector(".pe-msg");if(m)m.textContent=d.fields[k];}});}
      if(d.outline){peData=d.outline;peRender();}
      peSay(d.error||"Could not save.","err");return null;
    }
    peData=d;peFile=d.file;peRender();peSay(d.message||"","okmsg");return d;
  }catch(e){peSay("Could not reach the Studio.","err");return null;}
  finally{peBusy=false;}
}
async function openPages(s,file){
  peFolder=s.folder;peFile=file||"index.html";$("pe-target").textContent=s.name;
  $("pe-panel").classList.remove("hidden");window.scrollTo({top:0,behavior:"smooth"});
  peSay("Loading...");
  try{
    const d=await (await fetch("/api/page?folder="+encodeURIComponent(s.folder)+"&file="+encodeURIComponent(peFile))).json();
    if(d.error){peSay(d.error,"err");return;}
    peData=d;peFile=d.file;peRender();peSay("");
  }catch(e){peSay("Could not load the page.","err");}
}
function peDirty(){
  const out=[];
  document.querySelectorAll("#pe-body [data-fid] .pe-in").forEach(inp=>{
    if(inp.value!==inp.dataset.orig)out.push({id:inp.closest("[data-fid]").dataset.fid,value:inp.value});});
  return out;
}
function peField(f){
  const w=peMk("div","pe-field");w.dataset.fid=f.id;
  const row=peMk("div","pe-row"),col=peMk("div");
  const lab=peMk("label",null,f.label);col.appendChild(lab);
  const inp=f.multiline?document.createElement("textarea"):document.createElement("input");
  inp.className="pe-in";inp.value=f.value;inp.dataset.orig=f.value;col.appendChild(inp);
  if(f.link){
    const dl=document.createElement("select");dl.className="pe-pick";
    dl.appendChild(new Option("Pick a page or section...",""));
    (peData.targets||[]).forEach(t=>dl.appendChild(new Option(t,t)));
    dl.onchange=()=>{if(dl.value){inp.value=dl.value;dl.value="";}};col.appendChild(dl);
  }
  if(f.preview){
    const img=peMk("img","pe-thumb");img.alt="";
    const show=()=>{img.src=/^https?:/i.test(inp.value)?inp.value:"/sites/"+peFolder+"/"+inp.value+"?v="+Date.now();};
    show();inp.addEventListener("change",show);row.appendChild(img);
    const up=peMk("label","btn-alt mini picbtn","Upload picture");up.style.margin="0";
    const fi=document.createElement("input");fi.type="file";fi.accept="image/*";fi.hidden=true;
    fi.onchange=async()=>{
      const file=fi.files[0];if(!file)return;
      if(file.size>30*1024*1024){peSay("That picture is too large (30 MB maximum).","err");return;}
      peSay("Uploading picture...");
      try{
        const blob=await shrinkToJpeg(file);
        const r=await fetch("/api/page-image",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({folder:peFolder,hint:file.name.replace(/\.[^.]*$/,""),data:await blobToBase64(blob)})});
        const d=await r.json();
        if(!r.ok){peSay(d.error||"Could not save the picture.","err");return;}
        inp.value=d.path;show();peSay("Picture uploaded. Press Save changes to use it.","okmsg");
      }catch(e){peSay("That file could not be read as a picture.","err");}
    };
    up.appendChild(fi);col.appendChild(up);
  }
  row.appendChild(col);
  w.appendChild(row);w.appendChild(peMk("div","hint err pe-msg"));
  if(f.remove){
    const rm=peMk("button","btn-alt mini","Remove this");rm.type="button";rm.style.marginTop=".3rem";
    rm.onclick=async()=>{if(confirm("Remove this from the page?"))
      await peCall("/api/page-edit",{file:peFile,hash:peData.hash,fields:peDirty().concat([{id:f.remove,value:true}])});};
    w.appendChild(rm);
  }
  return w;
}
function peRender(){
  const d=peData,tabs=$("pe-tabs"),body=$("pe-body");
  tabs.textContent="";body.textContent="";
  d.pages.forEach(p=>{
    const b=peMk("button","btn-alt mini"+(p.file===d.file?" on":""),p.title+(p.editable?"":" (shop)"));b.type="button";
    b.onclick=()=>{if(p.file!==d.file&&(peDirty().length===0||confirm("Leave this page? Unsaved changes will be lost.")))openPages({folder:peFolder,name:$("pe-target").textContent},p.file);};
    tabs.appendChild(b);
  });
  $("pe-undo").disabled=!d.can_undo;
  const cur=d.pages.find(p=>p.file===d.file);
  $("pe-delpage").classList.toggle("hidden",!(cur&&cur.deletable));
  if(d.generated){body.appendChild(peMk("p","hint","This page is built by the shop. Use Edit shop items to change it."));
    $("pe-save").disabled=true;return;}
  $("pe-save").disabled=false;
  const pg=peMk("div","pe-block");pg.appendChild(peMk("h3",null,"Page settings"));
  d.page_fields.forEach(f=>pg.appendChild(peField(f)));body.appendChild(pg);
  const addBar=(after)=>{
    const bar=peMk("div","pe-add"),sel=document.createElement("select");
    d.templates.forEach(t=>sel.appendChild(new Option(t.label,t.key)));
    const b=peMk("button","btn-alt mini","+ Add section here");b.type="button";
    b.onclick=()=>peCall("/api/page-edit",{file:peFile,hash:d.hash,fields:peDirty(),op:{action:"add",template:sel.value,after:after}});
    bar.appendChild(sel);bar.appendChild(b);return bar;
  };
  let prev=-1;
  d.blocks.forEach(b=>{
    if(b.role==="footer")body.appendChild(addBar(prev));
    const box=peMk("div","pe-block"+(b.pinned?" pe-shared":""));
    const head=peMk("div","pe-head");head.appendChild(peMk("h3",null,b.label));
    const acts=peMk("div","acts");
    const op=(t,o,c)=>{const x=peMk("button","btn-alt mini",t);x.type="button";x.onclick=async()=>{
      if(c&&!confirm(c))return;
      await peCall("/api/page-edit",{file:peFile,hash:d.hash,fields:peDirty(),op:Object.assign({i:b.i},o)});};acts.appendChild(x);};
    if(!b.pinned){
      if(b.up)op("Move up",{action:"move",dir:-1});
      if(b.down)op("Move down",{action:"move",dir:1});
      if(!b.managed)op("Duplicate",{action:"duplicate"});
      op("Delete section",{action:"delete"},"Delete the section \""+b.label+"\"? You can press Undo afterwards.");
    }
    head.appendChild(acts);box.appendChild(head);
    if(b.pinned)box.appendChild(peMk("p","hint",d.file==="index.html"?"Shared by every page. Changes here are copied to all pages.":"Shared by every page. Edit it on the Home page."));
    if(b.managed)box.appendChild(peMk("p","hint","The product list in this section comes from Edit shop items. Only the text around it is editable here."));
    b.fields.forEach(f=>box.appendChild(peField(f)));
    body.appendChild(box);
    if(b.i!=null&&b.role!=="header"){prev=b.i;}
    if(b.role==="header")body.appendChild(addBar(-1));
    else if(!b.pinned)body.appendChild(addBar(b.i));
  });
}
$("pe-save").onclick=async()=>{
  const f=peDirty();if(!f.length){peSay("No changes to save.");return;}
  await peCall("/api/page-edit",{file:peFile,hash:peData.hash,fields:f});
};
$("pe-undo").onclick=async()=>{if(confirm("Go back to the previous version of this page?"))await peCall("/api/page-undo",{file:peFile});};
$("pe-view").onclick=()=>window.open("/sites/"+peFolder+"/"+peFile,"_blank");
$("pe-close").onclick=()=>$("pe-panel").classList.add("hidden");
$("pe-addpage").onclick=async()=>{
  const d=await peCall("/api/page-add",{title:$("pe-newpage").value,menu:$("pe-inmenu").checked});
  if(d)$("pe-newpage").value="";
};
$("pe-delpage").onclick=async()=>{
  if(!confirm("Delete the page "+peFile+" and its menu links? You can recover it from the .edit_history folder."))return;
  await peCall("/api/page-delete",{file:peFile});
};

let pkSite=null;
const PK={stripe:{label:"Stripe",saved:"Stripe key"},usps:{label:"USPS",saved:"USPS details"}};
function pkEl(w){return $("pk-"+w+"-panel");}
function pkSay(w,t,cls){const e=pkEl(w).querySelector(".pk-status");e.className="hint pk-status "+(cls||"");e.textContent=t||"";}
function pkWhere(x){return x.source==="site"?"this site's own key":x.source==="shared"?"the shared key":"not set";}
async function pkLoad(w){
  const d=await (await fetch("/api/site-keys?folder="+encodeURIComponent(pkSite.folder))).json();
  if(d.error){pkSay(w,d.error,"err");return null;}
  const x=d[w];let t=PK[w].label+": ";
  if(!x.set)t+="not set";
  else t+="working ("+pkWhere(x)+", "+x.masked+(w==="stripe"&&x.mode?", "+x.mode+" mode":"")+")";
  pkEl(w).querySelector(".pk-state").textContent=t+"  |  "+(d.has_shop?"Shop pages: installed":"Shop pages: not added yet");
  if(w==="usps")pkEl(w).querySelector(".pk-zip").placeholder=x.origin_zip?"saved: "+x.origin_zip:"";
  return d;
}
async function openKeys(s,w){
  pkSite=s;["stripe","usps"].forEach(k=>pkEl(k).classList.add("hidden"));
  const el=pkEl(w);el.querySelector(".pk-target").textContent=s.name;pkSay(w,"");
  el.querySelectorAll(".pk-f").forEach(i=>{i.value="";});
  el.querySelector(".pk-state").textContent="Loading...";
  el.classList.remove("hidden");window.scrollTo({top:0,behavior:"smooth"});
  try{await pkLoad(w);}catch(e){pkSay(w,"Could not load.","err");}
}
async function pkPost(w,body){
  const r=await fetch("/api/site-keys",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(Object.assign({folder:pkSite.folder,which:w},body))});
  const d=await r.json();
  if(!r.ok){pkSay(w,d.error||"Could not save.","err");return null;}
  return d;
}
["stripe","usps"].forEach(w=>{
  const el=pkEl(w);
  el.querySelector(".pk-save").onclick=async()=>{
    pkSay(w,"Saving...");
    try{
      const body={action:"save"};el.querySelectorAll(".pk-f").forEach(i=>{body[i.dataset.k]=i.value.trim();});
      const d=await pkPost(w,body);if(!d)return;
      el.querySelectorAll(".pk-f").forEach(i=>{i.value="";});
      const cur=await pkLoad(w);
      let msg="Saved: "+d.saved.join(", ")+".";
      if(cur&&!cur[w].set)msg+=" The keys were saved, but the Studio could not confirm they are active. Check them in the "+PK[w].label+" dashboard.";
      pkSay(w,msg,"okmsg");
      if(cur&&!cur.has_shop){pkSay(w,msg+" Adding the shopping cart to this site...","okmsg");begin("/api/extras",{folder:pkSite.folder,action:"shop"});}
    }catch(e){pkSay(w,"Could not reach the Studio.","err");}
  };
  el.querySelector(".pk-import").onclick=async()=>{
    try{const d=await pkPost(w,{action:"import"});if(!d)return;await pkLoad(w);pkSay(w,"Copied the shared "+PK[w].label+" keys into this site.","okmsg");}
    catch(e){pkSay(w,"Could not reach the Studio.","err");}
  };
  el.querySelector(".pk-clear").onclick=async()=>{
    if(!confirm("Remove the "+PK[w].label+" keys saved for "+pkSite.name+"? The shared keys (if any) will be used instead."))return;
    try{const d=await pkPost(w,{action:"clear"});if(!d)return;await pkLoad(w);pkSay(w,"This site's "+PK[w].label+" keys were removed.","okmsg");}
    catch(e){pkSay(w,"Could not reach the Studio.","err");}
  };
  el.querySelector(".pk-close").onclick=()=>el.classList.add("hidden");
});

/* ---------- shared helpers for the three new panels ---------- */
async function api(method,url,body){
  const r=await fetch(url,{method,headers:{"Content-Type":"application/json"},body:body?JSON.stringify(body):undefined});
  const d=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(d.error||"Something went wrong.");
  return d;
}
function say(id,t,cls){const e=$(id);e.className="hint "+(cls||"");e.textContent=t||"";}
function blockEditor(host,blocks){
  host.textContent="";
  const list=peMk("div");host.appendChild(list);
  function add(b){
    const box=peMk("div","blk");box.dataset.type=b.type;
    const hd=peMk("div","hd");hd.appendChild(peMk("span",null,{heading:"Heading",text:"Text",image:"Picture",button:"Button",divider:"Divider line"}[b.type]));
    const acts=peMk("div","acts");
    [["Up",()=>{if(box.previousSibling)list.insertBefore(box,box.previousSibling);}],
     ["Down",()=>{if(box.nextSibling)list.insertBefore(box.nextSibling,box);}],
     ["Remove",()=>box.remove()]].forEach(([t,f])=>{const x=peMk("button","btn-alt mini",t);x.type="button";x.onclick=f;acts.appendChild(x);});
    hd.appendChild(acts);box.appendChild(hd);
    const field=(cls,label,val,multi,ph)=>{const l=peMk("label",null,label);const i=document.createElement(multi?"textarea":"input");
      i.className=cls;i.value=val||"";if(ph)i.placeholder=ph;l.appendChild(i);box.appendChild(l);return i;};
    if(b.type==="heading")field("f-text","Heading",b.text,false);
    if(b.type==="text")field("f-text","Text (leave a blank line between paragraphs)",b.text,true);
    if(b.type==="image"){
      const src=field("f-src","Picture (a path like assets/site/photo.jpg, or an https:// address)",b.src,false);
      const up=peMk("label","mini btn-alt picbtn","Upload a picture");const fi=document.createElement("input");fi.type="file";fi.accept="image/*";fi.hidden=true;
      fi.onchange=async()=>{const f=fi.files[0];if(!f)return;try{const blob=await shrinkToJpeg(f);
        const d=await api("POST","/api/page-image",{folder:blockEditor.folder,hint:f.name.replace(/\.[^.]*$/,""),data:await blobToBase64(blob)});src.value=d.path;}
        catch(e){alert(e.message||"That file could not be read as a picture.");}};
      up.appendChild(fi);box.appendChild(up);
      field("f-alt","Picture description",b.alt,false);
      if(host.dataset.kind==="news")field("f-link","Link when the picture is clicked (optional)",b.link,false);
      else field("f-caption","Caption (optional)",b.caption,false);
    }
    if(b.type==="button"){field("f-text","Button text",b.text,false);field("f-url","Where the button goes",b.url,false,"https://");}
    list.appendChild(box);
  }
  (blocks||[]).forEach(add);
  const bar=peMk("div","item-actions");
  [["heading","+ Heading"],["text","+ Text"],["image","+ Picture"],["button","+ Button"],["divider","+ Divider"]].forEach(([t,l])=>{
    const x=peMk("button","btn-alt mini",l);x.type="button";x.onclick=()=>add({type:t});bar.appendChild(x);});
  host.appendChild(bar);
  return ()=>Array.from(list.children).map(b=>{const q=c=>{const e=b.querySelector(c);return e?e.value:"";};
    const t=b.dataset.type;
    if(t==="heading"||t==="text")return{type:t,text:q(".f-text")};
    if(t==="image")return{type:t,src:q(".f-src"),alt:q(".f-alt"),link:q(".f-link"),caption:q(".f-caption")};
    if(t==="button")return{type:t,text:q(".f-text"),url:q(".f-url")};
    return{type:"divider"};});
}
/* ---------- Newsletter ---------- */
let nwSite=null,nwGet=null,nwId=null,nwPoll=null,nwData=null;
async function openNews(s){
  nwSite=s;blockEditor.folder=s.folder;$("nw-target").textContent=s.name;say("nw-status","Loading...");
  $("nw-panel").classList.remove("hidden");window.scrollTo({top:0,behavior:"smooth"});
  $("nw-editor").classList.add("hidden");await nwLoad();
}
async function nwLoad(){
  try{
    const d=await api("GET","/api/news?folder="+encodeURIComponent(nwSite.folder));nwData=d;const st=d.settings;
    say("nw-status",d.missing.length?"Still needed before sending: "+d.missing.join("; ")+".":"Email is set up and ready to send.",d.missing.length?"":"okmsg");
    $("nw-installed").textContent=d.installed?"The sign-up form is on the website (in the footer of every page).":"The website has no working sign-up form yet.";
    $("nw-install").textContent=d.installed?"Refresh the sign-up form":"Add the sign-up form to the website";
    const ps=$("nw-preset");if(!ps.options.length)Object.keys(d.presets).forEach(k=>ps.appendChild(new Option(d.presets[k].label,k)));
    ps.value=st.preset in d.presets?st.preset:"other";
    $("nw-host").value=st.smtp_host;$("nw-port").value=st.smtp_port;$("nw-sec").value=st.smtp_security==="ssl"?"ssl":"starttls";
    $("nw-user").value=st.smtp_user;$("nw-pass").value="";$("nw-pass").placeholder=d.password_set?"saved (leave empty to keep)":"";
    $("nw-fromname").value=st.from_name;$("nw-fromemail").value=st.from_email;$("nw-reply").value=st.reply_to;$("nw-url").value=st.site_url;
    $("nw-limit").value=st.daily_limit;$("nw-accent").value=st.accent;$("nw-postal").value=st.postal_address.split(", ").join("\n");
    $("nw-double").checked=!!st.double_opt_in;
    $("nw-sync").classList.toggle("hidden",!d.cloud);
    $("nw-counts").textContent=d.counts.active+" subscribed, "+d.counts.pending+" waiting to confirm, "+d.counts.unsubscribed+" unsubscribed.";
    $("nw-export").href="/api/news-export?folder="+encodeURIComponent(nwSite.folder);
    const box=$("nw-subs");box.textContent="";
    if(d.subscribers.length){const t=peMk("table","subs");d.subscribers.forEach(r=>{const tr=peMk("tr");
      tr.appendChild(peMk("td",null,r.email));const c=peMk("td");c.appendChild(peMk("span","pill",r.status));tr.appendChild(c);
      const a=peMk("td");
      if(r.status!=="unsubscribed"){const u=peMk("button","btn-alt mini","Unsubscribe");u.type="button";u.onclick=()=>nwSub({op:"unsubscribe",sub_id:r.id});a.appendChild(u);}
      const x=peMk("button","btn-alt mini","Delete");x.type="button";x.onclick=()=>{if(confirm("Delete "+r.email+" from the list?"))nwSub({op:"remove",sub_id:r.id});};a.appendChild(x);
      tr.appendChild(a);t.appendChild(tr);});box.appendChild(t);}
    const cb=$("nw-camps");cb.textContent="";
    d.campaigns.forEach(c=>{const row=peMk("div","blk");const hd=peMk("div","hd");
      hd.appendChild(peMk("span",null,c.subject+" "));hd.appendChild(peMk("span","pill",c.status+(c.status!=="draft"?" ("+c.sent+" sent"+(c.failed?", "+c.failed+" failed":"")+(c.queued?", "+c.queued+" waiting":"")+")":"")));
      const acts=peMk("div","acts");
      const btn=(t,f)=>{const x=peMk("button","btn-alt mini",t);x.type="button";x.onclick=f;acts.appendChild(x);};
      btn(c.status==="draft"?"Edit":"View / copy",()=>nwEdit(c.id,c.status!=="draft"));
      if(c.status==="partial"||c.status==="sending")btn("Continue sending",()=>nwSend(c.id));
      btn("Delete",async()=>{if(!confirm("Delete this newsletter?"))return;try{await api("POST","/api/news",{folder:nwSite.folder,action:"delete-campaign",id:c.id});nwLoad();}catch(e){say("nw-send-status",e.message,"err");}});
      hd.appendChild(acts);row.appendChild(hd);cb.appendChild(row);});
    if(d.sending&&d.sending.running){say("nw-send-status",d.sending.message);if(!nwPoll)nwPoll=setInterval(nwLoad,2000);}
    else{if(nwPoll){clearInterval(nwPoll);nwPoll=null;}if(d.sending&&d.sending.message&&d.sending.campaign)say("nw-send-status",d.sending.message,d.sending.stopped?"err":"okmsg");}
  }catch(e){say("nw-status",e.message,"err");}
}
async function nwSub(o){try{const d=await api("POST","/api/news",Object.assign({folder:nwSite.folder,action:"sub"},o));say("nw-sub-status",d.message,"okmsg");nwLoad();}catch(e){say("nw-sub-status",e.message,"err");}}
$("nw-install").onclick=async()=>{try{say("nw-status","Working...");const d=await api("POST","/api/news",{folder:nwSite.folder,action:"install"});await nwLoad();say("nw-installed",d.message,"okmsg");}catch(e){say("nw-status",e.message,"err");}};
$("nw-preset").onchange=()=>{const p=nwData&&nwData.presets[$("nw-preset").value];if(!p)return;if(p.host)$("nw-host").value=p.host;$("nw-port").value=p.port;$("nw-sec").value=p.security;$("nw-limit").value=p.limit;};
$("nw-save").onclick=async()=>{
  try{await api("POST","/api/news",{folder:nwSite.folder,action:"save-settings",password:$("nw-pass").value,settings:{
    preset:$("nw-preset").value,smtp_host:$("nw-host").value,smtp_port:$("nw-port").value,smtp_security:$("nw-sec").value,smtp_user:$("nw-user").value,
    from_name:$("nw-fromname").value,from_email:$("nw-fromemail").value,reply_to:$("nw-reply").value,site_url:$("nw-url").value,
    daily_limit:$("nw-limit").value,accent:$("nw-accent").value,postal_address:$("nw-postal").value,double_opt_in:$("nw-double").checked}});
    say("nw-set-status","Saved.","okmsg");nwLoad();}catch(e){say("nw-set-status",e.message,"err");}};
$("nw-test").onclick=async()=>{say("nw-set-status","Connecting...");try{const d=await api("POST","/api/news",{folder:nwSite.folder,action:"test-connection"});say("nw-set-status",d.message,"okmsg");}catch(e){say("nw-set-status",e.message,"err");}};
$("nw-sync").onclick=async()=>{say("nw-sub-status","Syncing...");try{const d=await api("POST","/api/news",{folder:nwSite.folder,action:"sync"});say("nw-sub-status",d.message,"okmsg");nwLoad();}catch(e){say("nw-sub-status",e.message,"err");}};
$("nw-confirm").onclick=async()=>{say("nw-sub-status","Sending...");try{const d=await api("POST","/api/news",{folder:nwSite.folder,action:"confirm-pending"});say("nw-sub-status",d.message,"okmsg");nwLoad();}catch(e){say("nw-sub-status",e.message,"err");}};
$("nw-import-go").onclick=()=>nwSub({op:"import",emails:$("nw-import").value,consent:$("nw-consent").checked});
async function nwEdit(id,readonly){
  nwId=readonly?null:id;let c={subject:"",blocks:[{type:"heading",text:""},{type:"text",text:""}]};
  if(id){try{c=await api("GET","/api/news-campaign?folder="+encodeURIComponent(nwSite.folder)+"&id="+id);}catch(e){say("nw-send-status",e.message,"err");return;}}
  $("nw-subject").value=c.subject;$("nw-blocks").dataset.kind="news";nwGet=blockEditor($("nw-blocks"),c.blocks);
  $("nw-editor").classList.remove("hidden");
  if(readonly)say("nw-send-status","This newsletter was already sent. Saving makes a new draft copy.");
}
$("nw-new").onclick=()=>nwEdit(null,false);
$("nw-cancel").onclick=()=>$("nw-editor").classList.add("hidden");
async function nwSave(){const d=await api("POST","/api/news",{folder:nwSite.folder,action:"save-campaign",id:nwId,subject:$("nw-subject").value,blocks:nwGet()});nwId=d.id;return d;}
$("nw-draft").onclick=async()=>{try{await nwSave();say("nw-send-status","Draft saved.","okmsg");nwLoad();}catch(e){say("nw-send-status",e.message,"err");}};
$("nw-sendtest").onclick=async()=>{try{say("nw-send-status","Sending the test...");const d=await api("POST","/api/news",{folder:nwSite.folder,action:"send-test",to:$("nw-testto").value,subject:$("nw-subject").value,blocks:nwGet()});say("nw-send-status",d.message,"okmsg");}catch(e){say("nw-send-status",e.message,"err");}};
async function nwSend(id){
  try{const d=await api("POST","/api/news",{folder:nwSite.folder,action:"send",id});say("nw-send-status",d.message);$("nw-editor").classList.add("hidden");
    if(!nwPoll)nwPoll=setInterval(nwLoad,2000);nwLoad();}catch(e){say("nw-send-status",e.message,"err");}
}
$("nw-send").onclick=async()=>{
  try{const d=await nwSave();
    if(!confirm("Send this newsletter to everyone who is subscribed ("+(nwData?nwData.counts.active:"?")+" people)? It cannot be unsent."))return;
    await nwSend(d.id);}catch(e){say("nw-send-status",e.message,"err");}
};
$("nw-close").onclick=()=>{$("nw-panel").classList.add("hidden");if(nwPoll){clearInterval(nwPoll);nwPoll=null;}};
/* ---------- Blog ---------- */
let bgSite=null,bgGet=null,bgId=null;
async function openBlog(s){bgSite=s;blockEditor.folder=s.folder;$("bg-target").textContent=s.name;say("bg-status-msg","");
  $("bg-panel").classList.remove("hidden");$("bg-editor").classList.add("hidden");window.scrollTo({top:0,behavior:"smooth"});await bgLoad();}
async function bgLoad(){
  try{const d=await api("GET","/api/blog?folder="+encodeURIComponent(bgSite.folder));
    $("bg-installed").textContent=d.installed?"The Blog page is on the website and in the menu.":"The website has no Blog page yet.";
    $("bg-install").textContent=d.installed?"Rebuild the Blog pages":"Add the Blog to the website (page and menu link)";
    const box=$("bg-posts");box.textContent="";
    if(!d.posts.length)box.appendChild(peMk("p","hint","No posts yet."));
    d.posts.forEach(p=>{const row=peMk("div","blk"),hd=peMk("div","hd");hd.appendChild(peMk("span",null,p.title+"  ("+p.date+") "));
      hd.appendChild(peMk("span","pill",p.status));const a=peMk("div","acts");
      const b=(t,f)=>{const x=peMk("button","btn-alt mini",t);x.type="button";x.onclick=f;a.appendChild(x);};
      b("Edit",()=>bgEdit(p.id));
      b("Delete",async()=>{if(!confirm("Delete this post?"))return;try{await api("POST","/api/blog",{folder:bgSite.folder,action:"delete-post",id:p.id});bgLoad();}catch(e){say("bg-status-msg",e.message,"err");}});
      hd.appendChild(a);row.appendChild(hd);box.appendChild(row);});
  }catch(e){say("bg-status-msg",e.message,"err");}
}
$("bg-install").onclick=async()=>{try{say("bg-status-msg","Working...");const d=await api("POST","/api/blog",{folder:bgSite.folder,action:"install"});say("bg-status-msg",d.message,"okmsg");bgLoad();}catch(e){say("bg-status-msg",e.message,"err");}};
async function bgEdit(id){
  bgId=id;let p={title:"",date:new Date().toISOString().slice(0,10),status:"draft",cover:"",excerpt:"",blocks:[{type:"text",text:""}]};
  if(id){try{p=await api("GET","/api/blog-post?folder="+encodeURIComponent(bgSite.folder)+"&id="+id);}catch(e){say("bg-status-msg",e.message,"err");return;}}
  $("bg-title").value=p.title;$("bg-date").value=p.date;$("bg-status").value=p.status;$("bg-cover").value=p.cover||"";$("bg-excerpt").value=p.excerpt||"";
  $("bg-blocks").dataset.kind="blog";bgGet=blockEditor($("bg-blocks"),p.blocks);$("bg-editor").classList.remove("hidden");
}
$("bg-new").onclick=()=>bgEdit(null);$("bg-cancel").onclick=()=>$("bg-editor").classList.add("hidden");
$("bg-cover-file").onchange=async e=>{const f=e.target.files[0];e.target.value="";if(!f)return;
  try{const blob=await shrinkToJpeg(f);const d=await api("POST","/api/page-image",{folder:bgSite.folder,hint:"cover",data:await blobToBase64(blob)});$("bg-cover").value=d.path;}
  catch(err){say("bg-status-msg",err.message||"That file could not be read as a picture.","err");}};
$("bg-save").onclick=async()=>{
  try{const d=await api("POST","/api/blog",{folder:bgSite.folder,action:"save-post",post:{id:bgId,title:$("bg-title").value,date:$("bg-date").value,
    status:$("bg-status").value,cover:$("bg-cover").value,excerpt:$("bg-excerpt").value,blocks:bgGet()}});
    bgId=d.id;say("bg-status-msg",d.message,"okmsg");bgLoad();}catch(e){say("bg-status-msg",e.message,"err");}};
$("bg-close").onclick=()=>$("bg-panel").classList.add("hidden");
/* ---------- Publish to Cloudflare ---------- */
let cfSite=null;
async function openCf(s){cfSite=s;$("cf-target").textContent=s.name;say("cf-status","");$("cf-panel").classList.remove("hidden");window.scrollTo({top:0,behavior:"smooth"});await cfLoad();}
async function cfLoad(){
  try{const d=await api("GET","/api/deploy?folder="+encodeURIComponent(cfSite.folder));
    $("cf-account").value=d.account_id;$("cf-project").value=d.project||cfSite.folder;$("cf-domain").value=d.domain;
    $("cf-token").value="";$("cf-token").placeholder=d.token_set?"saved ("+d.token_masked+"), leave empty to keep":"";
    $("cf-node").textContent=d.node?"Node.js was found on this computer.":"Node.js was NOT found on this computer yet.";
    $("cf-last").textContent=d.last_url?"Last published "+d.last_deploy+": "+d.last_url:"Not published yet.";
    $("cf-warn").textContent=d.needs_server.length?"Cloudflare Pages hosts files only, so these do not work there yet: "+d.needs_server.join("; ")+".":"";
  }catch(e){say("cf-status",e.message,"err");}
}
$("cf-save").onclick=async()=>{try{await api("POST","/api/deploy",{folder:cfSite.folder,action:"save",account_id:$("cf-account").value.trim(),project:$("cf-project").value.trim(),token:$("cf-token").value.trim(),domain:$("cf-domain").value.trim()});say("cf-status","Saved.","okmsg");cfLoad();}catch(e){say("cf-status",e.message,"err");}};
$("cf-check").onclick=async()=>{say("cf-status","Checking...");try{const d=await api("POST","/api/deploy",{folder:cfSite.folder,action:"check"});say("cf-status",d.message,"okmsg");}catch(e){say("cf-status",e.message,"err");}};
$("cf-domain-go").onclick=async()=>{say("cf-status","Working...");try{const d=await api("POST","/api/deploy",{folder:cfSite.folder,action:"domain"});say("cf-status",d.message,"okmsg");}catch(e){say("cf-status",e.message,"err");}};
$("cf-deploy").onclick=()=>begin("/api/deploy",{folder:cfSite.folder,action:"deploy"});
$("cf-close").onclick=()=>$("cf-panel").classList.add("hidden");
loadSites();loadIntegrations();
</script></body></html>
'''


class Handler(BaseHTTPRequestHandler):
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
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if url.path == "/api/sites":
            return self._send(200, list_sites())
        if url.path == "/api/status":
            query = parse_qs(url.query)
            job_id = (query.get("job") or [""])[0]
            job = JOBS.get(job_id)
            if not job:
                return self._send(404, {"error": "unknown job"})
            since = int((query.get("since") or ["0"])[0] or 0)
            with job.lock:
                lines = job.lines[since:]
                nxt = len(job.lines)
            return self._send(200, {"state": job.state, "lines": lines, "next": nxt,
                                    "site_url": job.site_url, "error": job.error})
        if url.path == "/api/integrations":
            if not self._host_ok():
                return self._send(403, {"error": "forbidden"})
            return self._send(200, integrations_status())
        if url.path == "/api/items":
            folder = (parse_qs(url.query).get("folder") or [""])[0]
            if not self._host_ok() or not re.fullmatch(r"[a-z0-9-]+", folder) \
                    or not (PROJECTS / folder / "index.html").is_file():
                return self._send(400, {"error": "Unknown site."})
            rows, warnings = shop_cart.items_for_form(PROJECTS / folder)
            return self._send(200, {"items": rows, "warnings": warnings,
                                    "path": str(PROJECTS / folder / "items.csv")})
        if url.path == "/api/site-keys":
            folder = (parse_qs(url.query).get("folder") or [""])[0]
            if not self._host_ok() or not re.fullmatch(r"[a-z0-9-]+", folder) \
                    or not (PROJECTS / folder / "index.html").is_file():
                return self._send(400, {"error": "Unknown site."})
            return self._send(200, site_secrets.status(PROJECTS / folder))
        if url.path in ("/api/news", "/api/news-campaign", "/api/news-export", "/api/blog", "/api/blog-post", "/api/deploy"):
            q = parse_qs(url.query)
            folder = (q.get("folder") or [""])[0]
            if not self._host_ok() or not re.fullmatch(r"[a-z0-9-]+", folder) \
                    or not (PROJECTS / folder / "index.html").is_file():
                return self._send(400, {"error": "Unknown site."})
            site = PROJECTS / folder
            try:
                if url.path == "/api/news":
                    return self._send(200, newsletter.overview(site))
                if url.path == "/api/news-campaign":
                    return self._send(200, newsletter.get_campaign(site, (q.get("id") or ["0"])[0]))
                if url.path == "/api/news-export":
                    data = newsletter.export_csv(site).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", f'attachment; filename="{folder}-subscribers.csv"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if url.path == "/api/blog":
                    return self._send(200, blog.overview(site))
                if url.path == "/api/blog-post":
                    return self._send(200, blog.get_post(site, (q.get("id") or [""])[0]))
                return self._send(200, deploy_cloudflare.status(site))
            except (ValueError, OSError) as exc:
                return self._send(400, {"error": str(exc) or "Could not read this."})
        if url.path == "/api/page":
            q = parse_qs(url.query)
            folder = (q.get("folder") or [""])[0]
            if not self._host_ok() or not re.fullmatch(r"[a-z0-9-]+", folder) \
                    or not (PROJECTS / folder / "index.html").is_file():
                return self._send(400, {"error": "Unknown site."})
            try:
                return self._send(200, site_editor.outline_for(PROJECTS / folder, (q.get("file") or ["index.html"])[0]))
            except (ValueError, OSError, UnicodeDecodeError) as exc:
                return self._send(400, {"error": str(exc) or "Could not read the page."})
        match = re.fullmatch(r"/sites/([a-z0-9-]+)/api/([a-z]+)", url.path)
        if match:
            return self._site_api(match.group(1), match.group(2), {})
        if url.path.startswith("/sites/"):
            return self._serve_site(unquote(url.path[len("/sites/"):]))
        self._send(404, {"error": "not found"})

    def _host_ok(self):
        """Refuse requests whose Host header is not this computer (blocks DNS-rebinding tricks)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _read_json(self, limit=65536):
        """JSON body, but only when the browser says it is JSON. A web page on another site
        cannot send that content type to us without our consent, so it cannot change settings."""
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > limit:
                return None
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _site_api(self, folder, route, body):
        """Shop API for a site being previewed here: /sites/<folder>/api/<route>."""
        site = PROJECTS / folder
        if not self._host_ok() or not (site / "index.html").is_file():
            return self._send(404, {"error": "not found"})
        base = shop_api.public_base("http", self.headers.get("Host", "127.0.0.1"), f"/sites/{folder}")
        status, data = shop_api.handle(site, route, body, base)
        self._send(status, data)

    def _serve_site(self, relative):
        target = (PROJECTS / relative).resolve()
        if PROJECTS not in target.parents and target != PROJECTS:
            return self._send(403, {"error": "forbidden"})
        if any(part.startswith(".") for part in Path(relative).parts):
            return self._send(403, {"error": "forbidden"})
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/integrations":
            data = self._read_json()
            if data is None or not self._host_ok():
                return self._send(400, {"error": "invalid request"})
            try:
                saved = save_integrations(data)
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            return self._send(200, {"saved": saved, "status": integrations_status()})
        match = re.fullmatch(r"/sites/([a-z0-9-]+)/api/([a-z]+)", path)
        if match:
            data = self._read_json()
            if data is None:
                return self._send(415, {"error": "JSON only"})
            return self._site_api(match.group(1), match.group(2), data)
        if path == "/api/items":
            data = self._read_json(limit=400000)
            folder = str((data or {}).get("folder", "")).strip()
            if data is None or not self._host_ok() or not re.fullmatch(r"[a-z0-9-]+", folder) \
                    or not (PROJECTS / folder / "index.html").is_file():
                return self._send(400, {"error": "Unknown site or invalid item list."})
            items, errors = shop_cart.clean_rows(PROJECTS / folder, data.get("items"))
            if errors:
                return self._send(400, {"error": "Check the items marked in red.", "rows": errors})
            shop_cart.save_items(PROJECTS / folder, items)
            return self._send(200, {"items": len(items)})
        if path == "/api/item-image":
            data = self._read_json(limit=6000000)
            folder = str((data or {}).get("folder", "")).strip()
            if data is None or not self._host_ok() or not re.fullmatch(r"[a-z0-9-]+", folder) \
                    or not (PROJECTS / folder / "index.html").is_file():
                return self._send(400, {"error": "Unknown site or invalid upload."})
            try:
                raw = base64.b64decode(str(data.get("data", "")), validate=True)
            except (ValueError, TypeError):
                return self._send(400, {"error": "The picture could not be read."})
            kind = (".jpg" if raw[:3] == b"\xff\xd8\xff" else ".png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "")
            if not kind or len(raw) > 4 * 1024 * 1024:
                return self._send(400, {"error": "Use a JPEG or PNG picture under 4 MB."})
            slug = re.sub(r"[^a-z0-9]+", "-", str(data.get("hint", "")).lower()).strip("-")[:30].strip("-") or "item"
            name = f"{slug}-{secrets.token_hex(3)}{kind}"
            target = PROJECTS / folder / "assets" / "shop"
            target.mkdir(parents=True, exist_ok=True)
            (target / name).write_bytes(raw)
            return self._send(200, {"path": f"assets/shop/{name}"})
        if path in ("/api/page-edit", "/api/page-add", "/api/page-delete", "/api/page-undo"):
            data = self._read_json(limit=1000000)
            folder = str((data or {}).get("folder", "")).strip()
            if data is None or not self._host_ok() or not re.fullmatch(r"[a-z0-9-]+", folder) \
                    or not (PROJECTS / folder / "index.html").is_file():
                return self._send(400, {"error": "Unknown site or invalid request."})
            if not BUILD_LOCK.acquire(blocking=False):
                return self._send(409, {"error": "A task is already running. Wait for it to finish."})
            site, file = PROJECTS / folder, str(data.get("file", "index.html"))
            try:
                if path == "/api/page-edit":
                    out = site_editor.edit(site, file, str(data.get("hash", "")), data.get("fields"), data.get("op"))
                elif path == "/api/page-add":
                    out = site_editor.add_page(site, str(data.get("title", "")), bool(data.get("menu", True)))
                elif path == "/api/page-delete":
                    out = site_editor.delete_page(site, file)
                else:
                    out = site_editor.undo(site, file)
                return self._send(200, out)
            except site_editor.EditError as exc:
                return self._send(400, {"error": str(exc), "fields": exc.fields})
            except site_editor.Conflict as exc:
                try:
                    fresh = site_editor.outline_for(site, file)
                except (ValueError, OSError):
                    fresh = None
                return self._send(409, {"error": str(exc), "outline": fresh})
            except (ValueError, OSError, UnicodeDecodeError) as exc:
                return self._send(400, {"error": str(exc) or "Could not change the page."})
            finally:
                BUILD_LOCK.release()
        if path == "/api/site-keys":
            data = self._read_json()
            folder = str((data or {}).get("folder", "")).strip()
            action = str((data or {}).get("action", ""))
            if data is None or not self._host_ok() or not re.fullmatch(r"[a-z0-9-]+", folder) \
                    or not (PROJECTS / folder / "index.html").is_file():
                return self._send(400, {"error": "Unknown site or invalid request."})
            site = PROJECTS / folder
            try:
                if action == "save":
                    saved = site_secrets.save(site, str(data.get("stripe_key", "")).strip(),
                                              str(data.get("usps_client_id", "")).strip(),
                                              str(data.get("usps_client_secret", "")).strip(),
                                              str(data.get("usps_origin_zip", "")).strip())
                    return self._send(200, {"saved": saved})
                which = str(data.get("which", ""))
                if which not in ("stripe", "usps"):
                    return self._send(400, {"error": "Unknown action."})
                if action == "import":
                    return self._send(200, {"copied": len(site_secrets.import_shared(site, which))})
                if action == "clear":
                    site_secrets.clear(site, which)
                    return self._send(200, {"ok": True})
                return self._send(400, {"error": "Unknown action."})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            except OSError:
                return self._send(500, {"error": "Could not write the key file in the site folder."})
        if path in ("/api/news", "/api/blog", "/api/deploy"):
            data = self._read_json(limit=2000000)
            folder = str((data or {}).get("folder", "")).strip()
            action = str((data or {}).get("action", ""))
            if data is None or not self._host_ok() or not re.fullmatch(r"[a-z0-9-]+", folder) \
                    or not (PROJECTS / folder / "index.html").is_file():
                return self._send(400, {"error": "Unknown site or invalid request."})
            site = PROJECTS / folder
            if path == "/api/deploy" and action == "deploy":
                if not BUILD_LOCK.acquire(blocking=False):
                    return self._send(409, {"error": "A task is already running. Wait for it to finish."})
                job = Job(f"{int(time.time() * 1000)}")
                JOBS[job.id] = job
                threading.Thread(target=run_deploy_job, args=(job, folder), daemon=True).start()
                return self._send(200, {"job": job.id})
            writes = action in ("install", "save-post", "delete-post", "rebuild")
            if writes and not BUILD_LOCK.acquire(blocking=False):
                return self._send(409, {"error": "A task is already running. Wait for it to finish."})
            try:
                if path == "/api/news":
                    nid = data.get("id")
                    if action == "install":
                        return self._send(200, {"message": " ".join(newsletter.install(site))})
                    if action == "save-settings":
                        newsletter.save_settings(site, data.get("settings") or {}, str(data.get("password", "")))
                        return self._send(200, {"message": "Saved."})
                    if action == "test-connection":
                        return self._send(200, {"message": newsletter.test_connection(site)})
                    if action == "save-campaign":
                        return self._send(200, {"id": newsletter.save_campaign(site, nid, data.get("subject"), data.get("blocks")),
                                                "message": "Draft saved."})
                    if action == "delete-campaign":
                        newsletter.delete_campaign(site, nid)
                        return self._send(200, {"message": "Deleted."})
                    if action == "send-test":
                        return self._send(200, {"message": newsletter.send_test(
                            site, str(data.get("to", "")).strip(), " ".join(str(data.get("subject", "")).split()) or "Test",
                            newsletter.clean_blocks(data.get("blocks"), newsletter.load_settings(site)["site_url"]))})
                    if action == "send":
                        return self._send(200, {"message": newsletter.start_send(site, nid)})
                    if action == "sync":
                        return self._send(200, {"message": newsletter.sync_cloud(site)})
                    if action == "confirm-pending":
                        return self._send(200, {"message": newsletter.send_confirmations(site)})
                    if action == "sub":
                        return self._send(200, {"message": newsletter.subscriber_op(
                            site, str(data.get("op", "")), data.get("sub_id"), data.get("emails", ""), bool(data.get("consent")))})
                elif path == "/api/blog":
                    if action == "install":
                        return self._send(200, {"message": " ".join(blog.install(site))})
                    if action == "save-post":
                        post = blog.save_post(site, data.get("post") or {})
                        note = ""
                        if (site / "blog.html").is_file():
                            note = " " + blog.rebuild(site)[0]
                        return self._send(200, {"id": post["id"], "message": "Saved." + note})
                    if action == "delete-post":
                        blog.delete_post(site, str(data.get("id", "")))
                        if (site / "blog.html").is_file():
                            blog.rebuild(site)
                        return self._send(200, {"message": "Deleted."})
                    if action == "rebuild":
                        return self._send(200, {"message": " ".join(blog.rebuild(site))})
                else:
                    if action == "save":
                        deploy_cloudflare.save_config(site, str(data.get("account_id", "")), str(data.get("project", "")),
                                                      str(data.get("token", "")), str(data.get("domain", "")))
                        return self._send(200, {"message": "Saved."})
                    if action == "check":
                        return self._send(200, {"message": deploy_cloudflare.check_connection(site)})
                    if action == "domain":
                        return self._send(200, {"message": deploy_cloudflare.add_domain(site)})
                return self._send(400, {"error": "Unknown action."})
            except (ValueError, OSError, UnicodeDecodeError) as exc:
                return self._send(400, {"error": str(exc) or "Could not do that."})
            finally:
                if writes:
                    BUILD_LOCK.release()
        if path == "/api/page-image":
            data = self._read_json(limit=6000000)
            folder = str((data or {}).get("folder", "")).strip()
            if data is None or not self._host_ok() or not re.fullmatch(r"[a-z0-9-]+", folder) \
                    or not (PROJECTS / folder / "index.html").is_file():
                return self._send(400, {"error": "Unknown site or invalid upload."})
            try:
                raw = base64.b64decode(str(data.get("data", "")), validate=True)
            except (ValueError, TypeError):
                return self._send(400, {"error": "The picture could not be read."})
            kind = (".jpg" if raw[:3] == b"\xff\xd8\xff" else ".png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "")
            if not kind or len(raw) > 4 * 1024 * 1024:
                return self._send(400, {"error": "Use a JPEG or PNG picture under 4 MB."})
            slug = re.sub(r"[^a-z0-9]+", "-", str(data.get("hint", "")).lower()).strip("-")[:30].strip("-") or "picture"
            name = f"{slug}-{secrets.token_hex(3)}{kind}"
            target = PROJECTS / folder / "assets" / "site"
            target.mkdir(parents=True, exist_ok=True)
            (target / name).write_bytes(raw)
            return self._send(200, {"path": f"assets/site/{name}"})
        if path == "/api/extras":
            data = self._read_json()
            folder = str((data or {}).get("folder", "")).strip()
            action = str((data or {}).get("action", "")).strip()
            if data is None or not self._host_ok() or not re.fullmatch(r"[a-z0-9-]+", folder) \
                    or not (PROJECTS / folder / "index.html").is_file() or action not in EXTRAS_ACTIONS:
                return self._send(400, {"error": "Unknown site or action."})
            if not BUILD_LOCK.acquire(blocking=False):
                return self._send(409, {"error": "A task is already running. Wait for it to finish."})
            job = Job(f"{int(time.time() * 1000)}")
            JOBS[job.id] = job
            threading.Thread(target=run_extras_job,
                             args=(job, folder, action, data.get("settings") or {}), daemon=True).start()
            return self._send(200, {"job": job.id})
        if path not in ("/api/build", "/api/expand", "/api/polish", "/api/mobile", "/api/critique"):
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "invalid request"})
        if not os.getenv("GROQ_API_KEY"):
            return self._send(400, {"error": "GROQ_API_KEY is not set."})
        if path == "/api/critique":
            folder = str(data.get("folder", "")).strip()
            if not re.fullmatch(r"[a-z0-9-]+", folder) or not (PROJECTS / folder / "index.html").is_file():
                return self._send(400, {"error": "Unknown site."})
            if not BUILD_LOCK.acquire(blocking=False):
                return self._send(409, {"error": "A task is already running. Wait for it to finish."})
            try:
                app = ProjectOrchestrator()
                project = _find_project(app, folder)
                result = (200, {"suggestion": CSSPolisher(project, store=app.store).critique()})
            except Exception as exc:
                result = (400, {"error": str(exc)})
            finally:
                BUILD_LOCK.release()
            return self._send(*result)
        if path in ("/api/expand", "/api/polish", "/api/mobile"):
            folder = str(data.get("folder", "")).strip()
            if not re.fullmatch(r"[a-z0-9-]+", folder) or not (PROJECTS / folder / "index.html").is_file():
                return self._send(400, {"error": "Unknown site."})
            if path == "/api/expand":
                target, args = run_expand_job, (folder,)
            elif path == "/api/mobile":
                target, args = run_mobile_job, (folder,)
            else:
                feedback = str(data.get("feedback", "")).strip()[:2000]
                undo = bool(data.get("undo", False))
                target, args = run_polish_job, (folder, feedback, undo, bool(data.get("lint_only", False)))
        else:
            brief = clean_brief(str(data.get("brief", "")))
            if len(brief) < 10:
                return self._send(400, {"error": "Please describe the website you want."})
            name = str(data.get("name", "")).strip()[:60] or name_from_brief(brief)
            target, args = run_job, (None, "", brief, bool(data.get("expand")), None)
        if not BUILD_LOCK.acquire(blocking=False):
            return self._send(409, {"error": "A task is already running."})
        job = Job(f"{int(time.time() * 1000)}")
        JOBS[job.id] = job
        if path == "/api/build":
            settings = data.get("settings") if isinstance(data.get("settings"), dict) else None
            args = (name, "", brief, bool(data.get("expand")), settings)
        threading.Thread(target=target, args=(job,) + args, daemon=True).start()
        self._send(200, {"job": job.id})


def free_port(start=8765):
    for port in range(start, start + 20):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("No free port found between 8765 and 8784.")


def main():
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Groq Website Studio running at {url}   (Ctrl-C to stop)", flush=True)
    if not os.getenv("GROQ_API_KEY"):
        print("WARNING: GROQ_API_KEY is not set; builds will be refused until it is.", flush=True)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
