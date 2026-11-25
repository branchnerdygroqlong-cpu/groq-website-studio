Qwen 8K output request test

Run from the project venv:
  cd ~/groq-website-studio
  source venv/bin/activate
  python test_qwen_8k_output.py

The script makes ONE direct API request to qwen/qwen3.8-27b with max_tokens=8000.
It bypasses Studio's router and does not alter files, config, or database.
It waits 65 seconds first. The prompt requests a long handbook so the model has
a reason to generate several thousand tokens.

Important distinctions:
- API accepts max_tokens=8000: the per-request parameter is accepted.
- finish_reason='length': generation reached a limit; inspect completion_tokens.
- finish_reason='stop': model ended before reaching the cap; this does not prove
  the account can sustain 8,000 output tokens/minute.
- 429 OTPM: organization output-token-per-minute quota blocked the request.
- 429 TPM: total input+output tokens/minute quota blocked the request.
- A model catalogue max_completion_tokens value is a model ceiling, not your
  organization's active rate quota.

This test can consume a substantial part of your quota if it succeeds.
Run once, then share the complete output (including any error).
