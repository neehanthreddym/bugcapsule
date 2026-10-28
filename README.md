# BugCapsule

**Share the bug. Keep the production data.**

BugCapsule is being built to turn a sensitive HAR (HTTP Archive) network capture into a shareable, runnable capsule: production data will be replaced with consistent synthetic values, authentication material will be removed locally, and deterministic verification will check that a supported API failure still reproduces from a local mock server. The exported capsule is intended to run without a production account, original customer records, or an AI account.

Built for the **Meta Global AI Developer Hackathon** (Oct 26 – Nov 4, 2026) · Indie Developer track · Theme: Push the Frontier (developer tooling) · Team: BugCapsule (solo)

## Status

🚧 Under active development during the hackathon (Oct 26 – Nov 4, 2026). All code and demonstration captures in this repository are created during the event, using synthetic data only.

Currently working:

- FastAPI backend health endpoint at `/health` and interactive API documentation at `/docs`.
- React frontend shell with a labeled native HAR file selector. Selecting a file does not upload or process it.

HAR parsing, authentication scrubbing, model integration, replacement, verification, and capsule export are **not implemented**. The workflow below describes the planned MVP.

## The problem

Developers share HAR files to investigate API failures, but those files can contain authentication tokens, cookies, and customer response data. Existing sanitizers strip secrets — but stripping alone doesn't prove the transformed capture still demonstrates the failure, and replacing every identifier with `[REDACTED]` destroys cross-request relationships (which IDs are equal, which are different), often killing the bug in the shared file.

## How it works (planned)

1. **Import** one HAR file, locally.
2. **Select** the relevant requests, confirm the failure to preserve, and inspect the exact outbound payload before any model call.
3. **Review** the transformation plan proposed by the agent.
4. **Build capsule** — deterministic local replacement + verification.
5. **Approve export and download** the runnable package: transformed capture, local mock/replay API, and a verification report.

## Where Meta AI does essential work

The planned Muse Spark 1.3 (Meta Model API) integration will run a bounded agentic workflow: it will inspect structural metadata and pseudonymized examples from the selected transactions, infer semantic relationships between fields across requests and responses (for example, `customer.id` in one response matching `owner_id` in a later request), propose relationship-preserving replacements and verification checks, and revise the plan when verification exposes broken relationships.

All replacements and verification will be performed by deterministic local code. The model's intended role is automatic interpretation of unfamiliar APIs; without it, field mappings would have to be configured by hand. Meta API access and exact API details are unverified and must be checked against official documentation before implementing the agent.

## MVP scope

**In scope**

- HAR files containing JSON REST responses; one selected API origin; at most 10 selected transactions
- Exactly three planned failure patterns: duplicate pagination records, missing/null fields, inconsistent identifier references
- Consistent replacement engine: equal original values stay equal, different values stay different
- Transformation preview with human review before export
- Local replay server from a fixed template, plus an exported verification report

**Out of scope**

- Authentication flows, GraphQL, WebSockets, binary/non-JSON responses
- Reproducing an entire application or frontend
- Automatic publication, model training/fine-tuning
- Any claim of guaranteed anonymization

## Privacy by design

These requirements apply to the planned processing pipeline; that pipeline is not implemented yet.

- Raw HAR files never leave your machine; original response bodies stay local.
- Authentication material (Authorization headers, cookies, tokens) is removed deterministically, locally, **before** any model call.
- Only structural metadata and pseudonymized examples are sent to the model, and the UI shows exactly what will be sent.
- Exported capsules contain only transformed data, and the verification report states its limits honestly.

## Tech stack

- **Backend:** Python, FastAPI, Pydantic
- **Frontend:** React, Vite
- **Model (planned):** Muse Spark 1.3 via Meta Model API; access and exact API details unverified
- **Replay:** fixed-template local mock server included in each exported capsule

## Setup

Prerequisites: Python 3.11 or later, plus Node.js and npm compatible with the frontend dependencies.

The existing Python virtual environment is `.venv/` at the repository root. Reuse it locally. On a fresh clone without that environment, create it from the repository root:

```sh
python3 -m venv .venv
```

Install backend dependencies from `backend/`, activating the root environment (macOS/Linux shell):

```sh
cd backend
source ../.venv/bin/activate
python -m pip install -e ".[dev]"
```

The `dev` extra includes pytest. `backend/.env.example` contains placeholder configuration names only; Meta credentials are not needed for the current shell. Keep real keys backend-only and never commit the real `.env`.

In a separate terminal, install frontend dependencies from the repository root:

```sh
cd frontend
npm ci
```

## Usage

Start the backend from the repository root in one terminal:

```sh
cd backend
source ../.venv/bin/activate
python -m uvicorn app.main:app --reload
```

- Open `http://127.0.0.1:8000/health`; the implemented endpoint returns `{"status":"ok"}`.
- Open `http://127.0.0.1:8000/docs` for FastAPI's interactive API documentation.

Start the frontend from the repository root in a second terminal:

```sh
cd frontend
npm run dev
```

Open the local URL printed by Vite. The page displays BugCapsule's title, tagline, and HAR file selector. Selecting a file only updates the native input; there is no file reading, upload, parsing, or backend processing.

Frontend checks run from `frontend/`:

```sh
npm run build
npm run lint
```

Backend checks run from `backend/` with the existing virtual environment activated, using `python -m pytest`. No test cases are implemented yet.

## Who it's for

- **Open-source maintainers** — receive a runnable API failure without access to the reporter's system
- **Frontend developers** — debug against recorded responses while the backend is unavailable
- **QA engineers** — turn a reported failure into a portable regression fixture
- **SDK authors** — investigate an unusual response without a customer account

## License

MIT — see [LICENSE](LICENSE).
