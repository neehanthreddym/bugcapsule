# Synthetic fixtures

All captures in this folder are fabricated/synthetic and contain no real user data.

| Fixture | Status | Expected failure pattern |
| --- | --- | --- |
| `duplicate-pagination.har` | Available | Duplicate pagination records |
| `missing-null-fields.har` | Planned | Missing/null fields |
| `inconsistent-references.har` | Planned | Inconsistent identifier references |

`duplicate-pagination.har` simulates a fictional customer-management frontend making
two successive GET requests to `https://api.example.test/v1/customers`, with
`page_size=3`. Page 1 returns IDs `[101, 102, 103]`; page 2 returns `[103, 104, 105]`.
Customer 103's complete record is identical on both pages. This overlap is the
only deliberately seeded failure; there is no total-count field.

Both requests deliberately include `Authorization: Bearer BUGCAPSULE_FAKE_TOKEN_DO_NOT_USE`
and `Cookie: session=BUGCAPSULE_FAKE_SESSION`, with a matching `request.cookies`
entry. These unmistakably fake markers are retained for later scrubber tests.
All payloads, names, companies, and timestamps are fabricated, all domains use
reserved `example.test` addresses, and no external service was contacted.
