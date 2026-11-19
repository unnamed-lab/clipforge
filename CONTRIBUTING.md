# Contributing

Clipforge is a small, local-first system with a deliberate split: Next.js renders the interface, and a Python worker owns every byte of media and every database write. Contributions should preserve that boundary.

Read [Architecture](docs/ARCHITECTURE.md), [Known limitations](docs/KNOWN_LIMITATIONS.md) and the [API reference](docs/API.md) before changing behaviour.

## Set up

```bash
corepack enable
pnpm install

cd apps/worker
py -3.13 -m venv .venv                          # Windows
.venv\Scripts\python -m pip install -e ".[dev]"
cd ../..
```

macOS / Linux:

```bash
cd apps/worker
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
cd ../..
```

Run the pair with `scripts/dev.ps1` (Windows) or `scripts/dev.sh`, or start them separately with `pnpm worker:dev` and `pnpm dev`. Confirm `node scripts/worker.mjs python` resolves the interpreter you installed into.

## Verify before you claim done

```bash
node scripts/verify.mjs            # typecheck, lint, shared/web tests, fast worker tests
node scripts/verify.mjs --build    # adds the production Next.js build
node scripts/verify.mjs --docker   # adds Compose build + smoke test
```

The gate fails fast and prints a per-step summary. A change is not complete because the happy path works; run the check that covers the code you touched.

- Worker tests: `pnpm test:worker`. Add `-- -m "not slow"` for the fast set. Tests marked `slow` run real faster-whisper inference and are not in CI.
- Web tests: `pnpm test`. Shared contracts: `pnpm --filter @clipforge/shared test`.
- Python style: Ruff with a 110-character line length.
- TypeScript: strict `tsc --noEmit`; no `any` without a written justification.

### UI changes

There is no browser automation in the gate. `playwright.config.ts` and `tests/e2e/smoke.spec.ts` exist, but `@playwright/test` is not a declared dependency, so `pnpm test:e2e` does not run on a clean install.

For any change that alters rendered UI:

1. Run both processes and open the affected route.
2. Exercise the changed flow, plus loading, empty, error and long-content states.
3. Check the console and network panel for errors you introduced.
4. Repeat at a 390×844 viewport for layout changes.
5. Say exactly what you checked and what you could not check.

Passing tests and a successful build are not evidence that the interface works.

## Architecture rules

### The browser never calls the worker

- Client components call relative `/api/...` URLs on the Next.js origin.
- `apps/web/src/app/api/[...path]/route.ts` proxies to the worker, injects `x-clipforge-token`, streams request and response bodies and forwards Range headers.
- Never expose the token, worker URL or a direct worker link in client code. No `NEXT_PUBLIC_*` secret.
- The worker binds `127.0.0.1:8788` by default. Keep it that way for host development.

### The worker is the only writer

- SQLite, media, FFmpeg, model files and the queue belong to the worker.
- The web app may read files only through worker HTTP routes.
- One worker process per data directory. The queue is a single-consumer FIFO.
- Do not add Redis, Postgres or a second worker to "scale" a problem that is not measured. The current topology is intentional; see [Storage and workers](docs/deployment/STORAGE_AND_WORKERS.md).

### Long work is a job

- HTTP handlers enqueue and return. Transcription, URL import and rendering never run inside a browser-facing request.
- Jobs report `state`, `stage`, `progress` and a user-safe `error_code` / `error_message`.
- A job handler must be safe to retry from the beginning; retry does not resume partial work.
- Check cancellation between expensive stages and around subprocesses.
- Do not swallow exceptions into a generic failure without logging the stack trace.

### Media and contracts

- Every source is validated with FFprobe. Trust the probe, not the extension or MIME header.
- Exports read the retained source, never the analysis proxy.
- Render decisions are stored in the render spec so the preview, the encode and the review read the same data.
- Request/response models live in `apps/worker/src/clipforge/api/models.py`; shared TypeScript contracts live in `packages/shared/src/`. Update both, the fixture and `apps/worker/tests/test_contract.py` together.
- Python and TypeScript must agree on literals. Generate from one list where practical; do not maintain two independent enums by hand.

### Data and schema

- `apps/worker/src/clipforge/db/schema.sql` is the schema for new databases.
- `Database._apply_column_migrations` in `db/connection.py` adds missing columns to existing databases. SQLite has no `ADD COLUMN IF NOT EXISTS`, so the helper inspects `PRAGMA table_info`.
- There is no migration framework. A destructive change needs an explicit migration path, a backup instruction and a schema-version bump. Do not drop or rewrite a user's data on startup.
- Runtime data (`data/`, generated media, `*.db`, model caches) must stay out of version control.

### Security

- Validate URLs before import and keep the SSRF checks ahead of `yt-dlp`.
- Sanitise filenames and never build a path from user input without resolving it inside the project workspace.
- Keep the worker token comparison constant-time.
- Do not add a provider that silently fabricates availability, metrics or confidence. Report unavailable explicitly.
- Optional external AI must remain opt-in, with consent recorded for the run that sends transcript text.
- Do not commit `.env`, tokens, model files, media, database files, logs or build output.

## Code conventions

### TypeScript / React

- PascalCase for components, types, interfaces and classes; camelCase for functions and variables; SCREAMING_SNAKE_CASE for module constants; kebab-case filenames.
- Prefer named exports. Keep `@/` imports for the web app and workspace imports for shared code.
- Prefer derived values over state plus effect. Use `useMemo` for expensive calculation, not for side effects.
- Handle promise rejection. No floating promises.
- Follow the existing Tailwind token vocabulary; do not introduce raw hex colours when a theme token exists.

### Python

- Type hints on public functions. `from __future__ import annotations`.
- Pydantic models for request and response boundaries; plain dataclasses or dicts internally where the codebase already does so.
- Keep FFmpeg invocation argv-based and shell-free. Never interpolate user text into a filter graph without escaping.
- Raise `AppError` subclasses or `JobFailed` with a stable code; do not leak stack traces to the client.
- Log with the module logger. Avoid logging transcript content or tokens.

### Tests

- Use pytest for the worker. Mark real media processing `integration` and real model inference `slow`.
- Use Vitest for web and shared packages.
- Structure a test by scenario: given the setup, when the action runs, then the observable result. One logical assertion per test.
- Mock external providers and the filesystem where the unit under test should not depend on them. Do not mock the unit itself.
- Cover the error and boundary cases: empty transcript, no audio, invalid span, disk pressure, cancelled job, unavailable provider, malformed URL.
- Never delete or skip a failing test to make a change pass.

## Adding a feature

Use this checklist so the change lands coherently:

- [ ] The behaviour has a defined boundary: what is supported, what degrades, what is refused.
- [ ] Request and response models validate at the edge; literals match the shared package.
- [ ] Long work is a job with stages, progress and a user-safe failure.
- [ ] The UI shows the real state: loading, empty, error, disabled, and the reason for a disabled action.
- [ ] New settings are added to `.env.example`, `Settings`, [Environment variables](docs/deployment/ENVIRONMENT_VARIABLES.md) and `/api/capabilities` where relevant.
- [ ] Data changes are additive or have a migration path.
- [ ] Tests cover the success path, the refusal path and the degradation.
- [ ] The docs contract below is updated.
- [ ] `node scripts/verify.mjs --build` passes; UI was exercised manually with both processes running.
- [ ] No generated media, model files, logs or secrets are staged.

## Documentation contract

Documentation describes the implementation, not the intent. Keep one authority per topic:

| Change | Update |
| --- | --- |
| Route, request field, response shape, error code | [docs/API.md](docs/API.md) |
| User-visible workflow or control | [docs/USER_GUIDE.md](docs/USER_GUIDE.md) |
| New limitation, degradation or absent feature | [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) |
| Environment variable or default | `.env.example`, [Environment variables](docs/deployment/ENVIRONMENT_VARIABLES.md), README table |
| Install, run or verification command | [README.md](README.md), [Local development](docs/deployment/LOCAL_DEVELOPMENT.md), this file |
| Architecture or data flow | [Architecture](docs/ARCHITECTURE.md), [Processing pipeline](docs/PROCESSING_PIPELINE.md) |

Rules:

- Say whether a capability is implemented, API-only, degraded, or absent.
- Do not describe a provider as working without a credential path and an explicit unavailable state.
- Do not claim browser verification, benchmark numbers or test results that were not produced.
- Dated audit documents are records. Add a newer record instead of silently rewriting history; update the reference docs for current behaviour.
- Update the [documentation index](docs/README.md) when adding or retiring a document.

## Commits and pull requests

- Keep the diff scoped. A bug fix does not carry a refactor.
- Write the commit message around the behaviour: what changed and why, not a file list.
- Include the verification you ran. If a check could not run, say why.
- Call out schema, config or protocol changes explicitly.
- Do not reformat unrelated files or reorder imports across the tree.
- Do not commit `.env`, `data/`, `node_modules/`, `.next/`, `.venv/`, `__pycache__/`, `*.db`, models or rendered media.

## Reporting security issues

Do not open a public issue for a vulnerability, leaked token or private media exposure. Report it privately to the repository owner with reproduction steps and the affected route or file. Rotate any exposed token immediately; the worker reads its token at startup, so restart after rotating.
