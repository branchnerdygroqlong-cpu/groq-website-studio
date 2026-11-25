# Groq Website Studio — Phase 1

This adds a modular foundation alongside your existing script. It does not overwrite `website_builder.py` or `generated_website/`.

## Install
Activate your existing virtual environment, then run:

```bash
pip install -r requirements-phase1.txt
export GROQ_API_KEY="your_actual_key"
```

## Create and manage projects

```bash
python studio_cli.py new "Solar Affiliate Website" --type affiliate --brief "Solar reviews and installer leads"
python studio_cli.py projects
python studio_cli.py task 1 "Create sitemap" --agent architecture
python studio_cli.py status 1
```

Persistent data is stored in `studio_data/studio.sqlite3`; project folders are under `projects/`.

## Test the Groq router

```python
from studio_core.groq_router import GroqRouter
router = GroqRouter()
reply = router.chat(
    "openai/gpt-oss-120b",
    [{"role":"user","content":"Say hello."}],
    estimated_out_tokens=150
)
print(reply.choices[0].message.content)
```

The model limits were carried forward from the prior script as starting estimates. Verify current limits and model availability in your Groq account.

Phase 1 provides persistent projects/tasks, a reusable orchestration interface, and centralized Groq routing. It does not yet implement the complete multi-agent website build, QA, or deployment pipeline.

The CLI runs in a terminal; Python files do not run natively inside Firefox.
