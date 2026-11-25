Groq Studio quota-aware router patch

Files replaced:
  studio_core/config.py
  studio_core/groq_router.py
  studio_core/storage.py

What changes:
- Qwen max_out is raised from 900 to 8000.
- The router subtracts already-recorded minute/day usage and estimated prompt
  tokens before calculating the request's max_tokens.
- It reserves 100 tokens under TPM as a safety margin.
- It records actual prompt_tokens and completion_tokens separately.
- Existing SQLite model_usage tables are migrated additively. Existing rows
  are retained; old record_usage(model, timestamp, tokens) calls still work.
- TPM remains combined input + output. This patch does NOT assume that the
  separate 1,000 OTPM message seen earlier is gone or that 8,000 OTPM is allowed.
  Groq remains the final authority and may still return 429.

Install:
  cd ~/groq-website-studio
  source venv/bin/activate
  cp studio_core/config.py studio_core/config.py.before_quota_router
  cp studio_core/groq_router.py studio_core/groq_router.py.before_quota_router
  cp studio_core/storage.py studio_core/storage.py.before_quota_router
  unzip -o ~/Downloads/groq_studio_quota_router_patch.zip
  python -m py_compile studio_core/config.py studio_core/groq_router.py studio_core/storage.py

Then verify:
  python - <<'PY'
  from studio_core.config import MODEL_LIMITS
  from studio_core.storage import Store
  from studio_core.groq_router import GroqRouter
  print("Qwen limits:", MODEL_LIMITS["qwen/qwen3.8-27b"])
  store = Store()
  print("Existing Qwen usage:", store.usage("qwen/qwen3.8-27b"))
  PY

The router's estimated prompt size is approximate; actual API usage is recorded
after successful calls. This patch does not change agent_runtime.py. To give
CSS a larger request budget, its call site must separately request a larger
estimated_out_tokens value; the router will still cap it against available TPM.
