# BugCapsule

**Share the bug. Keep the production data.**

BugCapsule turns a sensitive HAR (HTTP Archive) network capture into a shareable, runnable capsule: production data is replaced with consistent synthetic values, authentication material is removed locally, and a verification step proves the original API failure still reproduces from a local mock server — no production account, no original customer records, and no AI account needed to run the exported capsule.

Built for the **Meta Global AI Developer Hackathon** (Oct 26 – Nov 4, 2026) · Indie Developer track · Theme: Push the Frontier (developer tooling) · Team: BugCapsule (solo)

## The problem

Developers share HAR files to investigate API failures, but those files can contain authentication tokens, cookies, and customer response data. Existing sanitizers strip secrets — but stripping alone doesn't prove the transformed capture still demonstrates the failure, and replacing every identifier with `[REDACTED]` destroys cross-request relationships (which IDs are equal, which are different), often killing the bug in the shared file.

## How it works

1. **Import** one HAR file, locally.
2. **Select** the relevant requests and confirm the failure to preserve.
3. **Review** the transformation plan proposed by the agent.
4. **Build capsule** — deterministic local replacement + verification.
5. **Download** the runnable package: transformed capture, local mock/replay API, and a verification report.

## Where Meta AI does essential work

Muse Spark 1.3 (Meta Model API) runs a bounded agentic workflow: it inspects structural metadata and pseudonymized examples from the selected transactions, infers semantic relationships between fields across requests and responses (for example, `customer.id` in one response matching `owner_id` in a later request), proposes relationship-preserving replacements and verification checks, and revises the plan when verification exposes broken relationships.

All replacements and verification are performed by deterministic local code. Removing the model removes the automatic interpretation of unfamiliar APIs — every field mapping would have to be configured by hand.

## MVP scope

**In scope**
- HAR files containing JSON REST responses; one selected API origin; at most 10 selected transactions
- Three failure patterns: duplicate pagination records, missing/null fields, inconsistent identifier references
- Consistent replacement engine: equal original values stay equal, different values stay different
- Transformation preview with human review before export
- Local replay server from a fixed template, plus an exported verification report

**Out of scope**
- Authentication flows, GraphQL, WebSockets, binary/non-JSON responses
- Reproducing an entire application or frontend
- Automatic publication, model training/fine-tuning
- Any claim of guaranteed anonymization

## Privacy by design

- Raw HAR files never leave your machine; original response bodies stay local.
- Authentication material (Authorization headers, cookies, tokens) is removed deterministically, locally, **before** any model call.
- Only structural metadata and pseudonymized examples are sent to the model, and the UI shows exactly what will be sent.
- Exported capsules contain only transformed data, and the verification report states its limits honestly.

## Tech stack

- **Backend:** Python, FastAPI, Pydantic
- **Frontend:** React, Vite
- **Model:** Muse Spark 1.3 via Meta Model API
- **Replay:** fixed-template local mock server included in each exported capsule

## Status

🚧 Under active development during the hackathon (Oct 26 – Nov 4, 2026). All code and demonstration captures in this repository are created during the event, using synthetic data only.

## Who it's for

- **Open-source maintainers** — receive a runnable API failure without access to the reporter's system
- **Frontend developers** — debug against recorded responses while the backend is unavailable
- **QA engineers** — turn a reported failure into a portable regression fixture
- **SDK authors** — investigate an unusual response without a customer account

## License

MIT — see [LICENSE](LICENSE).
