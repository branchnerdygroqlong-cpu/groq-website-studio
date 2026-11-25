# Groq Website Studio — HTML assembler repair

This patch restores the missing active runtime and adds a dedicated HTML assembler.

## Included
- `studio_core/agent_runtime.py` — repaired PM/image/frontend orchestration.
- `studio_core/html_assembler.py` — CSS/HTML fragment validation and deterministic HTML5 assembly.

## Install
1. Extract this ZIP into the **Groq Website Studio project root** (the directory containing `studio_cli.py`).
2. Allow it to merge the `studio_core/` folder.
3. Do not rename or delete `studio_core/agent_runtime.py.bak`; it is retained as a backup.
4. From the project root, with the virtual environment active, run:

   ```bash
   python -m py_compile studio_core/agent_runtime.py studio_core/html_assembler.py
   python studio_cli.py projects
   ```

5. Use the existing project ID shown by `projects`:

   ```bash
   python studio_cli.py build PROJECT_ID --brief "Describe the website you want built"
   ```

## What changed
- The PM plans ordered page sections; it no longer plans document-opening/closing chunks.
- CSS is generated as a separate stylesheet (`styles.css`).
- Frontend responses contain section fragments only.
- The assembler creates `index.html` with a complete HTML5 document wrapper.
- CSS braces/quotes and HTML tag nesting are checked before files are published.
- Groq responses with `finish_reason == "length"` are rejected as truncated.
- Existing `index.html` is not replaced until all generated sections pass validation.
- Project status uses the actual `Store.set_project_status()` method.

## Notes
- No API key or model configuration changes are included.
- Existing model names and quota settings in `config.py` are preserved.
- A successful local mock build and truncation-protection test were run. No live Groq API call was made as part of patch testing.
