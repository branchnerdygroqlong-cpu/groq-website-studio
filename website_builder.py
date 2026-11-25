import os
import json
import time
import webbrowser
from pathlib import Path
import requests
from groq import Groq

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

MODELS = {
    "pm": "openai/gpt-oss-120b",
    "image_agent": "openai/gpt-oss-20b",
    "dev_agent": "qwen/qwen3.8-27b",
    "dev_fallback": "openai/gpt-oss-20b",
    "fast_worker": "allam-2-7b",
    "guard_strict": "meta-llama/llama-prompt-guard-2-86m",
    "guard_fast": "meta-llama/llama-prompt-guard-2-22m",
    "safeguard": "openai/gpt-oss-safeguard-20b",
}

# Exact rate limits from your Groq tier + tool-calling capability flags
MODEL_LIMITS = {
    "allam-2-7b": {
        "rpm": 30, "rpd": 7_000, "tpm": 6_000, "tpd": 500_000, "max_out": 1500, "supports_tools": False
    },
    "meta-llama/llama-prompt-guard-2-22m": {
        "rpm": 30, "rpd": 14_400, "tpm": 15_000, "tpd": 500_000, "max_out": 64, "supports_tools": False
    },
    "meta-llama/llama-prompt-guard-2-86m": {
        "rpm": 30, "rpd": 14_400, "tpm": 15_000, "tpd": 500_000, "max_out": 64, "supports_tools": False
    },
    "openai/gpt-oss-120b": {
        "rpm": 30, "rpd": 1_000, "tpm": 8_000, "tpd": 200_000, "max_out": 2500, "supports_tools": True
    },
    "openai/gpt-oss-20b": {
        "rpm": 30, "rpd": 1_000, "tpm": 8_000, "tpd": 200_000, "max_out": 1800, "supports_tools": True
    },
    "openai/gpt-oss-safeguard-20b": {
        "rpm": 3, "rpd": 1_000, "tpm": 2_000, "tpd": 200_000, "max_out": 500, "supports_tools": False
    },
    "qwen/qwen3.8-27b": {
        "rpm": 30, "rpd": 1_000, "tpm": 8_000, "tpd": 200_000, "max_out": 1800, "supports_tools": True
    },
}

# Rolling 60-second usage log: {model_id: [(timestamp, total_tokens), ...]}
MINUTE_USAGE_LOG = {m: [] for m in MODEL_LIMITS}
# Daily usage tracker: {model_id: {"requests": int, "tokens": int}}
DAILY_USAGE_LOG = {m: {"requests": 0, "tokens": 0} for m in MODEL_LIMITS}

PROJECT_DIR = Path("./generated_website")
ASSETS_DIR = PROJECT_DIR / "assets"
PROJECT_DIR.mkdir(exist_ok=True)
ASSETS_DIR.mkdir(exist_ok=True)


def _estimate_input_tokens(messages: list, tools: list = None) -> int:
    """Estimates prompt + tool schema tokens (~4 chars per token)."""
    raw = json.dumps(messages, default=str)
    if tools:
        raw += json.dumps(tools, default=str)
    return max(1, len(raw) // 4)


def get_available_tpm(model: str) -> int:
    """Returns remaining Tokens-Per-Minute budget for a model right now."""
    now = time.time()
    MINUTE_USAGE_LOG[model] = [
        (ts, tok) for ts, tok in MINUTE_USAGE_LOG[model] if now - ts < 60.0
    ]
    used = sum(tok for _, tok in MINUTE_USAGE_LOG[model])
    return MODEL_LIMITS[model]["tpm"] - used


def paced_groq_call(
    model: str,
    messages: list,
    tools: list = None,
    estimated_out_tokens: int = 1000,
    fallback_models: list = None,
):
    """Enforces RPM, TPM (input + output), RPD, TPD, and tool support across all calls."""
    candidates = [model] + (fallback_models or [])
    if tools:
        candidates = [c for c in candidates if MODEL_LIMITS.get(c, {}).get("supports_tools", False)]
    if not candidates:
        candidates = [model]

    chosen_model = candidates[0]

    # Pick the first candidate with available daily quota and immediate TPM headroom
    for cand in candidates:
        lim = MODEL_LIMITS[cand]
        daily = DAILY_USAGE_LOG[cand]
        est_in = _estimate_input_tokens(messages, tools)
        safe_out = min(estimated_out_tokens, lim["max_out"])
        if daily["requests"] < lim["rpd"] and (daily["tokens"] + est_in + safe_out) <= lim["tpd"]:
            if get_available_tpm(cand) >= (est_in + safe_out):
                if cand != model:
                    print(f"  [Smart Router] Switching from {model} -> {cand} to avoid TPM wait.")
                chosen_model = cand
                break

    limits = MODEL_LIMITS.get(chosen_model, {"rpm": 30, "rpd": 1000, "tpm": 6000, "tpd": 200000, "max_out": 1000})
    est_in_tokens = _estimate_input_tokens(messages, tools)
    safe_max_tokens = min(
        estimated_out_tokens,
        limits["max_out"],
        max(256, limits["tpm"] - est_in_tokens - 200),
    )
    projected_total = est_in_tokens + safe_max_tokens

    now = time.time()
    MINUTE_USAGE_LOG[chosen_model] = [
        (ts, tok) for ts, tok in MINUTE_USAGE_LOG[chosen_model] if now - ts < 60.0
    ]
    used_tokens = sum(tok for _, tok in MINUTE_USAGE_LOG[chosen_model])
    used_reqs = len(MINUTE_USAGE_LOG[chosen_model])

    # Wait if adding this request would exceed RPM or total TPM
    if (used_tokens + projected_total > limits["tpm"] or used_reqs + 1 > limits["rpm"]) and MINUTE_USAGE_LOG[chosen_model]:
        oldest_ts = MINUTE_USAGE_LOG[chosen_model][0][0]
        wait_seconds = max(1.0, 61.0 - (now - oldest_ts))
        print(
            f"  [Rate Pacer] {chosen_model} at {used_tokens}/{limits['tpm']} TPM "
            f"({used_reqs}/{limits['rpm']} RPM). Waiting {wait_seconds:.1f}s..."
        )
        time.sleep(wait_seconds)

    kwargs = {
        "model": chosen_model,
        "messages": messages,
        "max_tokens": safe_max_tokens,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    response = client.chat.completions.create(**kwargs)

    # Record actual total tokens (prompt + completion) and daily counts
    actual_total = getattr(response.usage, "total_tokens", projected_total)
    MINUTE_USAGE_LOG[chosen_model].append((time.time(), actual_total))
    DAILY_USAGE_LOG[chosen_model]["requests"] += 1
    DAILY_USAGE_LOG[chosen_model]["tokens"] += actual_total
    return response


# ==========================================
# 0. SAFETY GUARDRAIL HELPER
# ==========================================

def check_prompt_safety(user_prompt: str) -> bool:
    """Uses Llama Prompt Guard 2 (15K TPM / 14.4K RPD) to screen incoming prompts."""
    try:
        response = paced_groq_call(
            model=MODELS["guard_strict"],
            messages=[{"role": "user", "content": user_prompt}],
            estimated_out_tokens=16,
            fallback_models=[MODELS["guard_fast"]],
        )
        verdict = (response.choices[0].message.content or "").strip().lower()
        if any(flag in verdict for flag in ("jailbreak", "malicious", "unsafe")):
            print(f"  [Safety Guard] Blocked prompt ({verdict}).")
            return False
    except Exception:
        pass
    return True


# ==========================================
# 1. WORKER TOOLS (File System & Image API)
# ==========================================

def search_and_download_wikimedia_image(query: str, filename: str) -> str:
    url = "https://commons.wikimedia.org/w/api.php"
    headers = {"User-Agent": "GroqWebsiteAgent/1.0 (local dev project)"}

    # Try exact query first, then automatically fall back to the longest single keyword
    words = [w for w in query.strip().split() if len(w) > 2]
    search_attempts = [query]
    if len(words) > 1:
        search_attempts.append(max(words, key=len))

    try:
        for attempt_query in search_attempts:
            params = {
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": f"filetype:bitmap {attempt_query}",
                "gsrnamespace": 6,
                "gsrlimit": 5,
                "prop": "imageinfo",
                "iiprop": "url",
            }
            res = requests.get(url, params=params, headers=headers, timeout=15).json()
            pages = res.get("query", {}).get("pages", {})
            if not pages:
                continue

            for page in pages.values():
                info = next(iter(page.get("imageinfo", [])), {})
                img_url = info.get("url", "")
                if img_url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    ext = Path(img_url).suffix.lower()
                    safe_name = f"{Path(filename).stem}{ext}"
                    save_path = ASSETS_DIR / safe_name

                    img_data = requests.get(img_url, headers=headers, timeout=15).content
                    save_path.write_bytes(img_data)
                    return f"SUCCESS: Downloaded '{safe_name}' -> use relative path 'assets/{safe_name}'. Move on to the next requested image."

        return (
            f"UNAVAILABLE: No public-domain image found for '{query}'. "
            "Do NOT retry this image—use a clean CSS/SVG gradient or placeholder instead and finish."
        )
    except Exception as e:
        return f"ERROR: {str(e)}. Do NOT retry—move on."


def append_website_file(filepath: str, chunk: str, mode: str = "a") -> str:
    target_path = (PROJECT_DIR / filepath).resolve()
    if not str(target_path).startswith(str(PROJECT_DIR.resolve())):
        return "Error: Access denied outside project folder."
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, mode, encoding="utf-8") as f:
        f.write(chunk + "
")
    return f"Wrote {len(chunk)} chars to {filepath} (mode={mode})."


def open_local_preview(entry_file: str = "index.html") -> str:
    target_path = (PROJECT_DIR / entry_file).resolve()
    if not target_path.exists():
        return f"Error: {entry_file} does not exist yet."
    webbrowser.open(target_path.as_uri())
    return f"Opened {target_path.as_uri()} in default browser."


def strip_markdown_fences(text: str) -> str:
    clean = text.strip()
    if clean.startswith("```"):
        clean = "
".join(clean.splitlines()[1:])
    if clean.endswith("```"):
        clean = "
".join(clean.splitlines()[:-1])
    return clean.strip()


# ==========================================
# 2. SPECIALIST SUB-AGENTS
# ==========================================

def run_image_sourcer_agent(image_brief: str) -> str:
    print(f"
[Image Agent - {MODELS['image_agent']}] Sourcing public-domain images...")

    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_and_download_wikimedia_image",
                "description": "Search Wikimedia Commons for a public domain image and save it to assets/.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Simple 1-2 word search keyword (e.g. 'solar panels')"},
                        "filename": {"type": "string", "description": "Clean local filename without spaces"},
                    },
                    "required": ["query", "filename"],
                },
            },
        }
    ]

    messages = [
        {
            "role": "system",
            "content": (
                "You are an Image Sourcing Specialist. Call `search_and_download_wikimedia_image` "
                "for each requested image using simple 1-2 word queries. "
                "NEVER retry a failed or unavailable image—immediately move to the next image or return your final summary list."
            ),
        },
        {"role": "user", "content": image_brief},
    ]

    for _ in range(6):
        response = paced_groq_call(
            model=MODELS["image_agent"],
            messages=messages,
            tools=tools,
            estimated_out_tokens=450,
            fallback_models=[MODELS["dev_agent"], MODELS["pm"]],
        )
        msg = response.choices[0].message
        messages.append(msg)

        if not msg.tool_calls:
            return msg.content

        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            print(f"  -> Searching Wikimedia for: {args['query']}")
            result = search_and_download_wikimedia_image(**args)
            print(f"     {result}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "name": tc.function.name,
                "content": result,
            })

    return "Image sourcing completed."


def run_chunked_frontend_dev(site_summary: str, chunks: list, available_assets: str) -> str:
    """Executes the PM's dynamic chunk list sequentially using Qwen (with 20b/allam fallback)."""
    total = len(chunks)
    print(f"
[Frontend Dev Agent - {MODELS['dev_agent']}] Building site in {total} dynamic chunks...")

    rolling_context = ""

    for idx, chunk_spec in enumerate(chunks, 1):
        title = chunk_spec.get("chunk_title", f"Chunk {idx}")
        instructions = chunk_spec.get("instructions", "")
        est_tokens = min(int(chunk_spec.get("estimated_tokens", 1200)), 1600)
        file_mode = "w" if idx == 1 else "a"

        print(f"  -> [{idx}/{total}] {title} (budget: ~{est_tokens} tokens)...")

        position_rule = (
            "This is the FIRST chunk: start with `<!DOCTYPE html>`, `<head>` (including Tailwind CDN `<script src='https://cdn.tailwindcss.com'></script>` and Google Fonts), and open `<body>`. Do NOT close `</body>` or `</html>`."
            if idx == 1
            else (
                "This is the FINAL chunk: write this section, include any `<script>` interactivity needed for the whole page, and properly close `</body></html>`."
                if idx == total
                else "This is a MIDDLE chunk: output ONLY the `<section>` or `<div>` HTML block for this part. Do NOT include `<html>`, `<head>`, `<body>`, or closing `</html>` tags."
            )
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "You are an expert Frontend Developer writing clean, modern HTML5 + Tailwind CSS. "
                    "Output ONLY raw valid HTML/JS code—NEVER wrap output in ```html fences or add explanations. "
                    "Use the local image paths listed in Available Local Images (or clean inline SVG/CSS backgrounds if none were downloaded). "
                    f"{position_rule}"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Overall Site Design:
{site_summary}

"
                    f"Available Local Images:
{available_assets}

"
                    f"Previous Sections Built Summary:
{rolling_context or 'None yet (this is the top of the page).'}

"
                    f"Current Chunk ({idx}/{total} - {title}):
{instructions}"
                ),
            },
        ]

        response = paced_groq_call(
            model=MODELS["dev_agent"],
            messages=messages,
            estimated_out_tokens=est_tokens,
            fallback_models=[MODELS["dev_fallback"], MODELS["fast_worker"]],
        )
        raw_html = response.choices[0].message.content or ""
        clean_html = strip_markdown_fences(raw_html)

        append_website_file("index.html", clean_html, mode=file_mode)
        rolling_context += f"- Completed {title}
"

    return f"Completed all {total} chunks into index.html."


# ==========================================
# 3. MAIN PROJECT MANAGER AGENT LOOP
# ==========================================

def run_project_manager():
    pm_tools = [
        {
            "type": "function",
            "function": {
                "name": "delegate_website_build",
                "description": (
                    "Call this immediately once the user approves the plan or tells you to build. "
                    "Break the website into an ordered list of small, self-contained HTML chunks (~1000-1400 tokens each) "
                    "so the Frontend Dev Agent can build any website cleanly within token limits."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_brief": {
                            "type": "string",
                            "description": "Concise list of public-domain images needed using simple 1-2 word subjects (e.g. 'solar panels', 'wind turbine').",
                        },
                        "site_design_summary": {
                            "type": "string",
                            "description": "Plain-text summary of color hex palette, typography, brand name, and tone (NO HTML code).",
                        },
                        "build_chunks": {
                            "type": "array",
                            "description": "Ordered list of 4 to 6 sequential page sections, each sized for ~1100-1400 output tokens.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "chunk_title": {"type": "string", "description": "Short name of section, e.g. 'Navbar & Hero'"},
                                    "instructions": {"type": "string", "description": "Plain-text content, layout, and interactive requirements for this section (NO HTML code)."},
                                    "estimated_tokens": {"type": "integer", "description": "Estimated output tokens between 900 and 1400."},
                                },
                                "required": ["chunk_title", "instructions", "estimated_tokens"],
                            },
                        },
                    },
                    "required": ["image_brief", "site_design_summary", "build_chunks"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "rebuild_with_revisions",
                "description": (
                    "Call this when the user requests changes after previewing the website. "
                    "Re-plans the updated site into ~1100-1400 token chunks incorporating the user's feedback."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "site_design_summary": {
                            "type": "string",
                            "description": "Updated plain-text design summary reflecting user feedback.",
                        },
                        "build_chunks": {
                            "type": "array",
                            "description": "Ordered list of sequential page sections incorporating the requested edits.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "chunk_title": {"type": "string"},
                                    "instructions": {"type": "string"},
                                    "estimated_tokens": {"type": "integer"},
                                },
                                "required": ["chunk_title", "instructions", "estimated_tokens"],
                            },
                        },
                    },
                    "required": ["site_design_summary", "build_chunks"],
                },
            },
        },
    ]

    pm_messages = [
        {
            "role": "system",
            "content": (
                "You are a decisive, general-purpose Web Project Manager. "
                "If the user's initial website idea needs clarification, ask 2-3 brief questions ONCE. "
                "As soon as the user answers OR says 'build', 'go ahead', 'skip questions', or approves a plan, "
                "IMMEDIATELY call `delegate_website_build` in that turn. "
                "CRITICAL PLANNING RULE: Decompose ANY website into 4 to 6 sequential `build_chunks` (each ~1000-1400 tokens) "
                "covering the full page from top (`<head>` + Hero) to bottom (Footer + `<script>`). "
                "Never put raw HTML inside your JSON arguments—describe each chunk clearly in plain text."
            ),
        }
    ]

    last_asset_summary = "No assets downloaded yet."

    print("=== Groq General-Purpose Autonomous Website Studio ===")
    print("Describe any website idea to the Project Manager (type 'exit' to quit).
")

    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in {"exit", "quit"}:
            break
        if not user_input:
            continue

        if not check_prompt_safety(user_input):
            print("
PM: Please rephrase your request so we can safely build your website.
")
            continue

        pm_messages.append({"role": "user", "content": user_input})

        while True:
            response = paced_groq_call(
                model=MODELS["pm"],
                messages=pm_messages,
                tools=pm_tools,
                estimated_out_tokens=2200,
                fallback_models=[MODELS["dev_agent"], MODELS["image_agent"]],
            )
            pm_msg = response.choices[0].message
            pm_messages.append(pm_msg)

            if not pm_msg.tool_calls:
                print(f"
PM ({MODELS['pm']}): {pm_msg.content}
")
                break

            for tc in pm_msg.tool_calls:
                args = json.loads(tc.function.arguments)

                if tc.function.name == "delegate_website_build":
                    print(f"
[PM Agent] Planned {len(args['build_chunks'])} dynamic chunks. Dispatching agents...")
                    last_asset_summary = run_image_sourcer_agent(args["image_brief"])
                    dev_summary = run_chunked_frontend_dev(
                        args["site_design_summary"],
                        args["build_chunks"],
                        last_asset_summary,
                    )
                    preview_status = open_local_preview("index.html")

                    tool_output = (
                        f"Build complete.
Image Agent Report: {last_asset_summary}
"
                        f"Dev Agent Report: {dev_summary}
Preview Status: {preview_status}"
                    )

                elif tc.function.name == "rebuild_with_revisions":
                    print(f"
[PM Agent] Re-chunking site into {len(args['build_chunks'])} sections with revisions...")
                    dev_summary = run_chunked_frontend_dev(
                        args["site_design_summary"],
                        args["build_chunks"],
                        last_asset_summary,
                    )
                    preview_status = open_local_preview("index.html")
                    tool_output = f"Revision complete: {dev_summary}. {preview_status}"

                pm_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.function.name,
                    "content": tool_output,
                })


if __name__ == "__main__":
    run_project_manager()
