#!/usr/bin/env python3
"""
Direct Groq diagnostic for qwen/qwen3.8-27b.

- Uses GROQ_API_KEY from the environment.
- Does NOT use GroqRouter or change the Studio database.
- Checks model metadata, then makes one controlled completion request.
- Requests 900 output tokens (below the 1,000 OTPM figure seen in the user's
  earlier error), and asks for a response long enough to reveal truncation.
"""
import json
import os
import sys
import time
from groq import Groq

MODEL = os.getenv("GROQ_TEST_MODEL", "qwen/qwen3.8-27b")
MAX_TOKENS = 900

def as_dict(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return str(obj)

def main():
    if not os.getenv("GROQ_API_KEY"):
        print("ERROR: GROQ_API_KEY is not set in this shell.")
        print("Run: export GROQ_API_KEY='your-key'")
        sys.exit(2)

    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    print(f"Model under test: {MODEL}")
    print(f"Requested max output tokens: {MAX_TOKENS}")
    print("This script bypasses Studio's local quota estimates and does not write to its database.")
    print()

    # Check model catalogue metadata if this endpoint is available.
    print("=== Model catalogue metadata ===")
    try:
        models = client.models.list()
        found = None
        for item in getattr(models, "data", []):
            if getattr(item, "id", None) == MODEL:
                found = item
                break
        if found is None:
            print("Model was not found in the current models.list() response.")
            print("Catalogue response may be filtered or this model may no longer be listed.")
        else:
            metadata = as_dict(found)
            # Print only fields useful for limits; never print credentials.
            keys = (
                "id", "context_window", "max_completion_tokens",
                "max_output_tokens", "created", "owned_by"
            )
            filtered = {k: metadata.get(k) for k in keys if k in metadata}
            print(json.dumps(filtered or metadata, indent=2, default=str))
    except Exception as exc:
        print(f"Could not retrieve catalogue metadata: {type(exc).__name__}: {exc}")
    print()

    # Give the prior minute's quota time to clear before the controlled call.
    print("Waiting 65 seconds before the single API test to reduce interference")
    print("from the earlier requests in the same output-token-per-minute window.")
    for remaining in range(65, 0, -1):
        print(f"\rStarting in {remaining:2d}s", end="", flush=True)
        time.sleep(1)
    print("\n")

    prompt = (
        "Write a technical explanation of how a responsive website stylesheet "
        "should be structured. Use exactly 12 numbered sections, each with a "
        "heading and 3 concise sentences. Cover reset, variables, typography, "
        "layout, navigation, hero, cards, forms, accessibility, hover/focus, "
        "mobile breakpoints, and maintainability. Do not stop early."
    )

    print("=== Direct completion test ===")
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise technical writer. Follow the requested "
                        "structure and provide the complete answer."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=MAX_TOKENS,
            temperature=0.2,
        )
        choice = response.choices[0]
        usage = getattr(response, "usage", None)
        print(f"Returned model: {getattr(response, 'model', 'unknown')}")
        print(f"Finish reason: {getattr(choice, 'finish_reason', 'unknown')}")
        if usage:
            print(f"Prompt tokens: {getattr(usage, 'prompt_tokens', 'unknown')}")
            print(f"Completion tokens: {getattr(usage, 'completion_tokens', 'unknown')}")
            print(f"Total tokens: {getattr(usage, 'total_tokens', 'unknown')}")
        content = choice.message.content or ""
        print(f"Response characters: {len(content)}")
        print(f"Numbered sections detected: {sum(1 for line in content.splitlines() if line.strip()[:1].isdigit() and '. ' in line.strip()[:6])}")
        print("\n=== Response preview ===")
        print(content[:5000])
        print("\n=== Interpretation ===")
        if choice.finish_reason == "length":
            print("The API accepted the 900-token request, but this completion hit its output cap.")
            print("This alone does NOT establish the model's absolute maximum completion size.")
        elif choice.finish_reason == "stop":
            print("The API accepted the 900-token request and the model ended normally.")
            print("This confirms 900 is accepted as a per-request cap in this test.")
        else:
            print("Inspect the finish reason and response above.")
    except Exception as exc:
        print(f"API TEST FAILED: {type(exc).__name__}: {exc}")
        print("\nIf this is a 429, note whether the message says OTPM, TPM, RPM, or another quota.")
        sys.exit(1)

if __name__ == "__main__":
    main()
