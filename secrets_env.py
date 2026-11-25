"""Server-side secret storage for Groq Website Studio.

API keys are written to a .env file next to studio_gui.py (the project root, which is
NOT the folder that gets served to browsers) and are only ever read by Python code on
the server. Nothing here returns a full secret to a web page: the GUI only ever sees a
masked form such as "****a1b2".

Be aware of what this is and is not: a .env file is plain text protected by file
permissions (chmod 600), by being outside the web root, and by being git-ignored.
Base64 or similar "encoding" would add no real protection (anyone who can read the file
can decode it), so it is deliberately not used. Anyone with access to your user account
on this computer can read the file.

Terminal use:
    python secrets_env.py status
    python secrets_env.py set STRIPE_SECRET_KEY        (prompts without echoing)
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ENV_PATH = Path(os.getenv("STUDIO_ENV_FILE") or (Path(__file__).resolve().parent / ".env"))

_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
# API keys are plain URL-safe text. Anything else (spaces, quotes, newlines, #) is refused,
# which also rules out injecting a second variable through a crafted value.
_VALUE = re.compile(r"^[A-Za-z0-9_\-./+=:@]{1,512}$")


def _parse(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def _read() -> dict:
    try:
        return _parse(ENV_PATH.read_text(encoding="utf-8"))
    except OSError:
        return {}


def get(name: str, default: str = "") -> str:
    """Current value: the .env file first (so a key saved in the GUI works at once,
    without restarting), then the process environment."""
    return _read().get(name) or os.environ.get(name, default) or default


def is_set(name: str) -> bool:
    return bool(get(name))


def mask(name: str) -> str:
    value = get(name)
    if not value:
        return ""
    return "****" + value[-4:] if len(value) >= 12 else "********"


def validate(name: str, value: str) -> str:
    if not _NAME.match(name or ""):
        raise ValueError(f"invalid setting name {name!r}")
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{name} is empty")
    if not _VALUE.match(value):
        raise ValueError(f"{name} contains characters that are not allowed in an API key "
                         "(spaces, quotes, # or line breaks); paste only the key itself")
    return value


def set_secret(name: str, value: str) -> None:
    """Add or replace one NAME=value line, keeping every other line (such as GROQ_API_KEY)
    exactly as it was. The file is replaced atomically and set to owner-only permissions."""
    value = validate(name, value)
    try:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=")
    out, replaced = [], False
    for line in lines:
        if pattern.match(line):
            if not replaced:
                out.append(f"{name}={value}")
                replaced = True
            continue                        # drop duplicates of the same name
        out.append(line)
    if not replaced:
        out.append(f"{name}={value}")
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=ENV_PATH.parent, prefix=".env-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(out) + "\n")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, ENV_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _ensure_gitignore()


def _ensure_gitignore() -> None:
    ignore = ENV_PATH.parent / ".gitignore"
    try:
        text = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        if ".env" not in {line.strip() for line in text.splitlines()}:
            ignore.write_text(text.rstrip("\n") + ("\n" if text else "") + ".env\n", encoding="utf-8")
    except OSError:
        pass


def redact(text, names=()) -> str:
    """Replace any stored secret value that appears in `text` (error messages, logs)."""
    text = str(text)
    stored = _read()
    for name in names or stored:
        value = stored.get(name) or os.environ.get(name, "")
        if value and len(value) >= 8:
            text = text.replace(value, "***")
    return text


def _main(argv):
    import getpass
    if len(argv) >= 2 and argv[1] == "status":
        for name in sorted(_read()):
            print(f"{name}: {mask(name) or '(empty)'}")
        return 0
    if len(argv) == 3 and argv[1] == "set":
        value = getpass.getpass(f"{argv[2]} (input is hidden): ")
        set_secret(argv[2], value)
        print(f"Saved {argv[2]} to {ENV_PATH} ({mask(argv[2])}).")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
