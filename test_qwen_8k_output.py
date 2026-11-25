#!/usr/bin/env python3
"""One-shot direct Groq test requesting 8,000 output tokens from Qwen."""
import os
import sys
import time
from groq import Groq

MODEL = os.getenv("GROQ_TEST_MODEL", "qwen/qwen3.8-27b")
REQUESTED_MAX_TOKENS = 8000

def main():
    key = os.getenv("GROQ_API_KEY")
    if not key:
        print("ERROR: GROQ_API_KEY is not set.")
        sys.exit(2)

    client = Groq(api_key=key)
    print(f"Model: {MODEL}")
    print(f"Requested max_tokens: {REQUESTED_MAX_TOKENS}")
    print("Direct API call: bypasses Studio router and local usage accounting.")
    print("This is one substantial generation and may consume a large share of your quota.")
    print()

    print("Waiting 65 seconds before the test to let the prior minute window clear.")
    for n in range(65, 0, -1):
        print(f"\rStarting in {n:2d}s", end="", flush=True)
        time.sleep(1)
    print("\nSending request...\n")

    prompt = """
Produce a detailed technical handbook with exactly 50 numbered sections.
Each section must have a descriptive heading and approximately 110-130 words.
Do not summarize, skip sections, or stop early. The sections should be distinct
and cover: semantic HTML, document structure, CSS reset, design tokens,
typography, color systems, spacing, responsive containers, CSS Grid, Flexbox,
navigation, accessible mobile menus, hero sections, image handling, cards,
feature grids, testimonials as a component pattern (do not invent real quotes),
forms, validation UI, buttons, links, focus indicators, hover states, motion
preferences, accessibility, contrast, screen readers, responsive breakpoints,
mobile-first design, tablet layouts, desktop layouts, performance, font loading,
image optimization, lazy loading, SEO metadata, headings, structured data,
security headers, content security policy, maintainable CSS, naming conventions,
component reuse, progressive enhancement, browser testing, visual regression,
deployment checks, analytics privacy, and post-launch maintenance.
Use complete paragraphs. Keep each section close to the requested word count.
"""

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a thorough technical author. Follow the exact "
                        "section count and provide the full requested handbook."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=REQUESTED_MAX_TOKENS,
            temperature=0.2,
        )
        choice = response.choices[0]
        usage = getattr(response, "usage", None)
        print("=== API RESULT ===")
        print("Returned model:", getattr(response, "model", "unknown"))
        print("Finish reason:", getattr(choice, "finish_reason", "unknown"))
        if usage:
            print("Prompt tokens:", getattr(usage, "prompt_tokens", "unknown"))
            print("Completion tokens:", getattr(usage, "completion_tokens", "unknown"))
            print("Total tokens:", getattr(usage, "total_tokens", "unknown"))
        content = choice.message.content or ""
        print("Response characters:", len(content))
        print("Numbered sections detected:",
              sum(1 for line in content.splitlines()
                  if line.strip().split(".", 1)[0].isdigit()
                  and ". " in line.strip()[:8]))
        print("\n=== FIRST 2500 CHARACTERS ===")
        print(content[:2500])
        print("\n=== INTERPRETATION ===")
        reason = getattr(choice, "finish_reason", None)
        if reason == "length":
            print("Groq accepted max_tokens=8000, and generation hit an output-length cap.")
        elif reason == "stop":
            print("Groq accepted max_tokens=8000, but the model stopped before exhausting it.")
            print("Completion token count shows how much it actually generated.")
        else:
            print("Review finish_reason and token counts above.")
    except Exception as exc:
        print("=== API REQUEST FAILED ===")
        print(type(exc).__name__ + ":", str(exc))
        print("\nCheck whether the error identifies OTPM, TPM, RPM, or a request parameter limit.")
        sys.exit(1)

if __name__ == "__main__":
    main()
