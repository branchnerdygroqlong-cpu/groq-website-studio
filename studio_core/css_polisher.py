"""Deterministic CSS repair + feedback-driven visual polish.

1. Free, deterministic repairs (no tokens): units on spacing variables, missing
   visually-hidden utility classes (see css_lint.py).
2. critique(): suggests plain-English fixes from the page's structure.
3. polish(): turns plain-English feedback into CSS overrides. The original stylesheet is
   wrapped once in a low-priority  @layer base ; every polish round is appended to a
   higher-priority  @layer polish , so it beats the original rules whatever their selector
   specificity (the generated CSS leans on #id selectors, which plain appended overrides
   cannot beat). The HTML is never touched.
4. Every action is backed up in polish_history/, so undo can step back repeatedly.

Run from the terminal (project root):
  python -m studio_core.css_polisher PROJECT_ID --feedback "Add more space between sections"
  python -m studio_core.css_polisher PROJECT_ID --lint-only
  python -m studio_core.css_polisher PROJECT_ID --critique
  python -m studio_core.css_polisher PROJECT_ID --undo
"""
from __future__ import annotations

import argparse
import os
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path

from .agent_runtime import _clean_output
from .config import MODELS
from .css_lint import fix_unitless_lengths, missing_utility_rules
from .groq_router import GroqRouter
from .html_assembler import validate_css
from .storage import Store

MARKER = "/* STUDIO-LAYERS */"
_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_VOID = {"img", "br", "hr", "input", "meta", "link", "source", "track", "wbr", "area", "col", "embed"}

DESIGN_PRINCIPLES = (
    "Principles: generous vertical rhythm (every top-level section gets at least 4rem of "
    "padding above and below); clear separation between sections (alternate two related "
    "background tones and/or a thin border or divider); content in a centered container "
    "with a sensible max-width and side padding; consistent gaps between cards and list "
    "items; a readable type scale and line length; never place a heading, intro text, "
    "list and media side by side as columns, since a section's parts normally stack, and "
    "side-by-side layouts are for 2-3 clearly parallel items such as cards or image plus "
    "text; everything stacks to one column on narrow screens; footers are tidy rows that "
    "wrap on small screens; interactive elements keep visible hover and focus states."
)


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".css")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Compact outline of the page structure (what the model "sees")
# --------------------------------------------------------------------------- #
class _Outline(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "root", "id": "", "classes": [], "children": [], "text": False}
        self.stack = [self.root]

    @staticmethod
    def _node(tag, attrs):
        a = dict((k, v or "") for k, v in attrs)
        return {"tag": tag, "id": a.get("id", ""), "classes": a.get("class", "").split(),
                "children": [], "text": False}

    def handle_starttag(self, tag, attrs):
        node = self._node(tag, attrs)
        self.stack[-1]["children"].append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1]["children"].append(self._node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data.strip():
            self.stack[-1]["text"] = True


def _label(node):
    return node["tag"] + (f"#{node['id']}" if node["id"] else "") + \
        "".join("." + c for c in node["classes"][:3])


def _render(nodes, depth, out, max_depth=5):
    i = 0
    while i < len(nodes):
        node, j = nodes[i], i
        while j + 1 < len(nodes) and _label(nodes[j + 1]) == _label(node):
            j += 1
        count = j - i + 1
        out.append("  " * depth + _label(node) + (f" x{count}" if count > 1 else "")
                   + (" (text)" if node["text"] and not node["children"] else ""))
        if node["children"] and depth < max_depth:
            _render(node["children"], depth + 1, out, max_depth)
        i = j + 1


def outline_html(html: str, limit: int = 4200) -> str:
    body = re.sub(r"<head\b.*?</head>|<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->",
                  "", html, flags=re.S | re.I)
    parser = _Outline()
    parser.feed(body)
    top = parser.root["children"]
    while len(top) == 1 and top[0]["tag"] in ("html", "body"):
        top = top[0]["children"]
    lines = []
    _render(top, 0, lines)
    return "\n".join(lines)[:limit]


def _html_classes_ids(html: str):
    classes, ids = set(), set()
    for m in re.finditer(r"""class\s*=\s*["']([^"']*)["']""", html):
        classes.update(m.group(1).split())
    ids.update(re.findall(r"""\sid\s*=\s*["']([^"']+)["']""", html))
    return classes, ids


def _check_polish_css(css: str, html: str) -> None:
    validate_css(css)
    if re.search(r"@layer|@import", css):
        raise ValueError("do not use @layer or @import; return plain CSS rules only")
    classes, ids = _html_classes_ids(html)
    cleaned = re.sub(r"url\([^)]*\)|\"[^\"]*\"|'[^']*'", "", _COMMENT.sub("", css))
    known, used = classes | ids, []
    for prelude in re.findall(r"([^{}]+)\{", cleaned):
        prelude = prelude.strip()
        if not prelude.startswith("@"):
            used += re.findall(r"[.#]([A-Za-z_][\w-]*)", prelude)
    unknown = sorted({u for u in used if u not in known})
    if used and len(unknown) > max(3, 0.25 * len(set(used))):
        raise ValueError("these classes/ids do not exist in the page: " + ", ".join(unknown[:8])
                         + "; target only classes and ids from the outline")


# --------------------------------------------------------------------------- #
# Stylesheet layering
# --------------------------------------------------------------------------- #
def ensure_layers(css: str) -> str:
    if MARKER in css:
        return css
    head = []

    def grab(match):
        head.append(match.group(0).strip())
        return ""
    body = re.sub(r"^\s*@(?:charset|import)\b[^;]*;", grab, css, flags=re.M)
    prefix = "\n".join(head) + "\n" if head else ""
    return (f"{prefix}{MARKER}\n@layer base, polish;\n@layer base {{\n{body.strip()}\n}}\n"
            "@layer polish {\n}\n")


def _polish_text(css: str) -> str:
    match = re.search(r"@layer polish \{(.*)\}\s*$", css, re.S)
    return match.group(1).strip() if match else ""


def _append_block(css: str, label: str, block: str) -> str:
    """Append a labelled block: into the polish layer if layered, else at the end."""
    stripped = css.rstrip()
    if MARKER in css:
        return stripped[:-1] + f"\n/* {label} */\n{block.strip()}\n}}\n"
    return stripped + f"\n\n/* {label} */\n{block.strip()}\n"


# --------------------------------------------------------------------------- #
# The agent
# --------------------------------------------------------------------------- #
class CSSPolisher:
    def __init__(self, project: dict, store: Store | None = None, router: GroqRouter | None = None):
        self.project = project
        self.store = store or Store()
        self.router = router or GroqRouter(self.store)
        self.root = Path(project["root_path"]).resolve()
        self.css_path = self.root / "styles.css"
        self.history = self.root / "polish_history"

    def _models(self) -> list[str]:
        return [MODELS["dev_fallback"], MODELS["pm"], MODELS["dev_agent"]]

    # ---- history ---------------------------------------------------------- #
    def _snapshots(self) -> list[Path]:
        return sorted(self.history.glob("styles.*.css")) if self.history.is_dir() else []

    @property
    def can_undo(self) -> bool:
        return bool(self._snapshots())

    def _snapshot(self) -> Path:
        self.history.mkdir(exist_ok=True)
        numbers = [int(p.stem.split(".")[1]) for p in self._snapshots() if p.stem.split(".")[1].isdigit()]
        path = self.history / f"styles.{max(numbers, default=0) + 1:03d}.css"
        path.write_text(self.css_path.read_text(encoding="utf-8"), encoding="utf-8")
        return path

    def undo(self) -> str:
        snapshots = self._snapshots()
        if not snapshots:
            raise RuntimeError("Nothing to undo: no earlier version of styles.css was saved.")
        last = snapshots[-1]
        _atomic_write(self.css_path, last.read_text(encoding="utf-8"))
        last.unlink()
        return (f"Restored styles.css from before the last change "
                f"({len(snapshots) - 1} earlier version(s) still available).")

    # ---- shared context --------------------------------------------------- #
    def _read(self):
        index_path = self.root / "index.html"
        if not index_path.is_file() or not self.css_path.is_file():
            raise RuntimeError("Project is missing index.html or styles.css.")
        return (index_path.read_text(encoding="utf-8"), self.css_path.read_text(encoding="utf-8"))

    @staticmethod
    def _tokens(css: str) -> str:
        match = re.search(r":root\s*\{.*?\}", _COMMENT.sub("", css), re.S)
        return " ".join((match.group(0) if match else "").split())[:1400]

    @staticmethod
    def _unstyled(html: str, css: str) -> list[str]:
        classes, _ids = _html_classes_ids(html)
        defined = set(re.findall(r"\.([A-Za-z_][\w-]*)", _COMMENT.sub("", css)))
        return sorted(classes - defined)

    # ---- critique --------------------------------------------------------- #
    def critique(self) -> str:
        """Plain-English suggestions from the page structure (the model cannot see the render)."""
        html, css = self._read()
        if hasattr(self.router, "reset_budget"):
            self.router.reset_budget()
        findings = list(fix_unitless_lengths(css)[1])
        unstyled = self._unstyled(html, css)
        if unstyled:
            findings.append("HTML classes with no CSS rule: " + ", ".join(unstyled[:10]))
        messages = [
            {"role": "system", "content": (
                "You are a senior UI designer reviewing a generated web page from its "
                "structure alone (you cannot see it). Write 3 to 6 short plain-English "
                "sentences that name specific sections and the concrete visual problems most "
                "likely to exist (cramped spacing, no separation between sections, parts of a "
                "section laid out side by side that should stack, a cluttered footer, weak "
                "hierarchy) and the fix for each. No code, no markdown, no greetings, and "
                "nothing you cannot infer from the outline.")},
            {"role": "user", "content": (
                f"Site: {self.project['name']}\n\nPage structure:\n{outline_html(html)}\n\n"
                f"Design tokens: {self._tokens(css) or 'none'}\n"
                f"Automatic findings: {'; '.join(findings) or 'none'}")},
        ]

        def validate(text):
            if len(text.strip()) < 60:
                raise ValueError("too short; give 3 to 6 specific sentences")
            if "{" in text or "```" in text:
                raise ValueError("no code or Markdown; plain sentences only")

        text, _model = self.router.generate(
            self._models(), messages, validate, estimated_out_tokens=600,
            label="Critique", clean=lambda raw: (raw or "").strip())
        return text

    # ---- polish ----------------------------------------------------------- #
    def polish(self, feedback: str = "", auto_lint_only: bool = False, aspects=None) -> str:
        if not self.css_path.is_file():
            raise RuntimeError("Project has no styles.css to polish.")
        index_path = self.root / "index.html"
        html = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
        original = self.css_path.read_text(encoding="utf-8")
        snapshot = self._snapshot()

        # 1. free deterministic repairs, saved immediately so a later model failure keeps them
        css, notes = fix_unitless_lengths(original)
        for note in notes:
            print(f"[polish] Fixed: {note}", flush=True)
        extra = missing_utility_rules(html, css) if html else ""
        if extra:
            css = _append_block(css, "UTILITIES", extra)
            print("[polish] Fixed: added missing visually-hidden utility class(es).", flush=True)
        if css != original:
            validate_css(css)
            _atomic_write(self.css_path, css)
        if auto_lint_only and not (feedback or "").strip():
            if css == original:
                snapshot.unlink(missing_ok=True)
                return "No automatic fixes were needed."
            return "Applied automatic CSS fixes (no AI used)."

        # 2. model-driven polish
        if hasattr(self.router, "reset_budget"):
            self.router.reset_budget()
        layered = ensure_layers(css)
        feedback = (feedback or "").strip()
        wanted = ", ".join(aspects or [])
        if not feedback and not wanted:
            feedback = "General visual polish: spacing, section separation, layout and readability."
        unstyled = self._unstyled(html, layered)
        previous = _polish_text(layered)[-2200:]
        user = (
            f"Site: {self.project['name']}\n"
            f"What the owner says is wrong: {feedback or '(see focus areas)'}\n"
            f"Focus areas: {wanted or 'none ticked'}\n\n"
            f"Page structure (tag#id.class, x N = repeated, (text) = has text):\n{outline_html(html)}\n\n"
            f"Existing design tokens (reuse them, keep the palette): {self._tokens(layered) or 'none'}\n"
            + (f"Classes in the HTML with no CSS rule at all: {', '.join(unstyled[:10])}\n" if unstyled else "")
            + (f"\nEarlier polish rules already applied (refine, do not repeat):\n{previous}\n" if previous else "")
        )
        messages = [
            {"role": "system", "content": (
                "You are an expert CSS designer polishing an existing website. Return ONLY "
                "plain CSS rules: no Markdown, no @layer, no @import, no explanations. Your "
                "CSS is loaded in a higher-priority layer, so it overrides the original "
                "stylesheet regardless of selector specificity: use simple selectors and "
                "restate the full property you are changing (for example flex-direction or "
                "grid-template-columns). Target ONLY classes and ids shown in the outline. "
                "Fix exactly what the owner describes first, then apply general polish. "
                "Every length needs a unit. Keep it under about 90 lines. " + DESIGN_PRINCIPLES)},
            {"role": "user", "content": user},
        ]
        print("[polish] Generating targeted CSS refinements ...", flush=True)
        try:
            block, model_used = self.router.generate(
                self._models(), messages, lambda text: _check_polish_css(text, html),
                estimated_out_tokens=1600, label="CSS Polish", clean=_clean_output, task_budget=12000)
        except Exception:
            if css == original:          # nothing changed: drop the pointless snapshot
                snapshot.unlink(missing_ok=True)
            raise
        number = len(re.findall(r"/\* POLISH \d+ \*/", layered)) + 1
        final = _append_block(layered, f"POLISH {number}", block)
        validate_css(final)
        _atomic_write(self.css_path, final)
        summary = f"Polish round {number} applied using {model_used}"
        if notes:
            summary += " (plus automatic spacing fixes)"
        print(f"[polish] {summary}", flush=True)
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Polish a generated project's CSS.")
    parser.add_argument("project_id", type=int, help="Project ID from `studio_cli.py projects`")
    parser.add_argument("--feedback", default="", help="Describe what looks wrong or what to improve")
    parser.add_argument("--lint-only", action="store_true", help="Only run the free deterministic fixes")
    parser.add_argument("--critique", action="store_true", help="Print suggested fixes; changes nothing")
    parser.add_argument("--undo", action="store_true", help="Step back to before the last change")
    args = parser.parse_args()
    store = Store()
    project = store.get_project(args.project_id)
    if not project:
        raise SystemExit(f"Project #{args.project_id} not found.")
    polisher = CSSPolisher(project, store=store)
    if args.undo:
        print(polisher.undo())
    elif args.critique:
        print(polisher.critique())
    else:
        print(polisher.polish(feedback=args.feedback, auto_lint_only=args.lint_only))


if __name__ == "__main__":
    main()
