# Phase 1.5 — Agent integration

This package adds an integrated Project Manager → Image Sourcer → Frontend Developer workflow to the Phase 1 foundation. It writes only inside the selected project's `root_path`, never the legacy `generated_website/` folder.

## Install/update
From `~/groq-website-studio`, with `venv` active:

```bash
unzip -o ~/Downloads/groq_phase1_5_agents.zip
```

The ZIP contains `studio_core/agent_runtime.py` and an updated `studio_core/cli.py`. Back up your current `studio_core/cli.py` first if you have made local edits.

## Configure API key
```bash
export GROQ_API_KEY="your_actual_groq_key"
```
This key is for the current terminal session. Do not paste it into chat or commit it to Git.

## Build test
Use the existing project #1:
```bash
python studio_cli.py build 1 --brief "Build a polished, responsive homepage for a solar-panel affiliate website. Explain the value proposition, compare solar panel options without inventing prices or ratings, include educational sections, clear affiliate disclosure placeholder, and calls to action."
```

This makes real Groq API calls and may consume quota. Wikimedia image lookup also requires internet access. Review image licensing before publishing.

## Verify
```bash
python studio_cli.py status 1
find projects/solar-affiliate-website -maxdepth 2 -type f -print
```
Expected files include `index.html`, `BUILD_REPORT.txt`, and optionally `assets/` images.

Open the page:
```bash
xdg-open projects/solar-affiliate-website/index.html
```

## Scope and safety
- This test builds one homepage, not the full multi-page engine yet.
- The task record persists. The two specialist tasks are currently audit/planning records; the main build task records completion/failure. More granular per-agent task transitions are a follow-up hardening item.
- The local usage ledger helps avoid exceeding configured estimates but is not authoritative Groq quota telemetry. Check actual limits in the Groq console.
- No production deployment is performed.
- Do not publish sourced images until their individual licenses and attribution requirements are verified.
