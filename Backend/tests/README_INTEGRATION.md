# Integration tests

## What are integration tests

Integration tests exercise **multiple subsystems** (API + workers + DB) with I/O replaced by mocks: TestClient or httpx against the FastAPI app, hitting `/discs/`, `/jobs/rip`, `/drives/`, etc. Workers run in-process or are mocked (`mock_celery`). All three mocks (MockDrive, MockMKV, test_db) are used where applicable. No real `/dev/sr*`, makemkvcon, or Postgres.

Examples: rip flows, /discs/, /drives/, /jobs/rip, postprocess, workflow step completion.

## How to run

```bash
pytest -m integration
```

Run from `Backend/` or from the project root with `tests/` in the Python path. The default `pytest` run still includes integration tests unless you explicitly exclude them (e.g. `pytest -m "not integration"`).

## When to use integration vs component vs E2E

| Layer        | Scope                        | Placement / mark                         |
|-------------|------------------------------|------------------------------------------|
| **Component** | One subsystem (e.g. /discs/ or /drives/ only) | `Backend/tests/component/`, `mock_drive` + `test_db` |
| **Integration** | Multiple subsystems, API + workers + DB, three mocks | `@pytest.mark.integration` in `Backend/tests/` |
| **E2E**       | Frontend + backend, Playwright | `README_E2E_TESTING.md`                  |

## `--run-integration`

The `--run-integration` flag is **reserved for tests that require network access** (e.g. TMDB scraper). It does **not** select the integration subset. To run only integration tests, use `pytest -m integration`.

## References

- `docs/TESTING.md` (development repo) — Integration section and testing pyramid
- `PHASE6_INTEGRATION_CHECKLIST.md` (historical) — Candidate tests and three-mocks audit
- [Backend/tests/README_E2E_TESTING.md](README_E2E_TESTING.md) — E2E framework
