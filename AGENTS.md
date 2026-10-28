# Development instructions

- Stack: Python 3.11+, FastAPI, and Pydantic backend; React/Vite frontend; pytest for backend tests.
- Run backend commands from `backend/` using the existing root virtual environment: `source ../.venv/bin/activate`. Install with `python -m pip install -e ".[dev]"`, start with `python -m uvicorn app.main:app --reload`, and run relevant tests with `python -m pytest`.
- Run frontend commands from `frontend/`: `npm ci`, `npm run dev`, `npm run build`, and `npm run lint`.
- Implement one requested step at a time. Preserve unrelated changes and avoid scope expansion.
- Frozen MVP: one HAR containing JSON REST transactions, one selected API origin, at most ten selected transactions, and exactly three failure patterns: duplicate pagination records, missing/null fields, and inconsistent identifier references.
- Exclude authentication flows, GraphQL, WebSockets, binary/non-JSON responses, complete application reproduction, model training/fine-tuning, and automatic publication.
- Raw HARs and original response bodies stay local. Remove authentication material deterministically and locally before any model call.
- `backend/app/privacy.py` exclusively builds outbound model payloads from locally scrubbed data: structural metadata and pseudonymized examples only. Users must be able to inspect the exact payload sent to the model.
- Muse Spark proposes plans and checks; deterministic local code performs replacements and verification. Equal original values must stay equal, and different original values must stay different. Require human approval before export.
- Verify official Meta API documentation before implementing the agent. Never invent endpoints, model identifiers, or SDK calls; API access and exact API details are currently unverified.
- Replay uses a fixed server template, never generated executable code.
- Never claim guaranteed anonymization.
- Keep private captures and generated outputs in ignored `/local/`. Keep other private HAR captures ignored; committed HAR fixtures under `fixtures/` must be entirely fabricated, with no real user data.
- Run checks relevant to the requested change and report actual results and failures. Distinguish user-confirmed history from newly run checks and planned work.
- Do not commit or push unless explicitly requested.
