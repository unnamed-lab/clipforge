# Clipforge documentation

This index separates current reference material from dated implementation records. When documents disagree, prefer the source code, `apps/worker/src/clipforge/db/schema.sql` and the worker's OpenAPI document.

## Start here

| If you want to… | Read |
| --- | --- |
| Install and run the app | [README quick start](../README.md#quick-start) and [Local development](deployment/LOCAL_DEVELOPMENT.md) |
| Use the product | [User guide](USER_GUIDE.md) |
| Call the worker directly | [API reference](API.md) |
| Understand the system | [Architecture](ARCHITECTURE.md) and [Processing pipeline](PROCESSING_PIPELINE.md) |
| Deploy it | [Docker](deployment/DOCKER.md) or [Render](deployment/RENDER.md) |
| Check whether a behaviour is supported | [Known limitations](KNOWN_LIMITATIONS.md) |

## Reference documents

| Document | Covers | Status |
| --- | --- | --- |
| [User guide](USER_GUIDE.md) | Import, analysis, candidate review, editing, packaging, export, diagnostics, troubleshooting | Current |
| [API reference](API.md) | Worker base URL, token auth, error envelope, route groups, job lifecycle, request examples | Current for the routes described; OpenAPI is authoritative |
| [Known limitations](KNOWN_LIMITATIONS.md) | Absent capabilities, operational constraints, verification policy | Current |
| [Architecture](ARCHITECTURE.md) | Service boundaries, storage rules, job model, security | Core shape current; data model predates later tables |
| [Processing pipeline](PROCESSING_PIPELINE.md) | Import through render and cleanup | Current for the core analysis path; editorial and packaging stages are documented separately |
| [Environment variables](deployment/ENVIRONMENT_VARIABLES.md) | Every supported setting | Authoritative |
| [Local development](deployment/LOCAL_DEVELOPMENT.md) | Host prerequisites, install, run, checks | Current |
| [Docker](deployment/DOCKER.md) | Compose services, volumes, Supabase caveats | Current |
| [Render](deployment/RENDER.md) | Blueprint topology, cost, limits | Current at the date written; confirm Render pricing |
| [Storage and workers](deployment/STORAGE_AND_WORKERS.md) | Storage backends, invariants, scaling path | Current |

## Dated records

These documents record a point-in-time investigation. They are useful evidence and rationale, not a description of every later change. Dates are in the filenames or headers.

| Document | Records |
| --- | --- |
| [Editorial intelligence audit](quality/EDITORIAL_INTELLIGENCE_AUDIT.md) | Duration presets, grounded hooks, narrative strategies, retention planning, quality gate (2026-11-17). |
| [AI packaging intelligence](quality/AI_PACKAGING_INTELLIGENCE.md) | Whole-transcript briefs, packaging director, title rubric, research layer, field-level regeneration (2026-11-18). |
| [AI packaging trend check](quality/AI_PACKAGING_TREND_CHECK.md) | Trend-provider policy and fallbacks; packaging sections superseded by the document above. |
| [First export failure analysis](quality/FIRST_EXPORT_FAILURE_ANALYSIS.md) | Composition, caption bounds, boundary review, EDL and validation work prompted by the first failed export. |
| [Video skills integration audit](quality/VIDEO_SKILLS_INTEGRATION_AUDIT.md) | Which external video-production techniques were adopted, adapted or rejected. |
| [Deployment audit](deployment/DEPLOYMENT_AUDIT.md) | Hosted deployment findings at the date recorded. |
| [Implementation plan](IMPLEMENTATION_PLAN.md) | Original sprint plan and milestone checklist (2026-11-15). Its status line is historical. |

## Documentation rules

1. **Describe the code that exists.** If a capability is only an interface, a stub, a disabled provider or an API-only route, say so.
2. **Separate measurement from intent.** Scores are editorial estimates, not engagement predictions. Quality-gate results come from FFprobe/FFmpeg evidence.
3. **Do not claim verification that did not happen.** Automated checks are listed in [Known limitations](KNOWN_LIMITATIONS.md#verification). UI changes require a manual pass against a running web and worker pair.
4. **Keep one authority per topic.** Configuration belongs in the environment table; routes belong in the API reference; product behaviour belongs in the user guide.
5. **Date audits.** A dated audit does not get silently rewritten when behaviour changes; add a newer record or update the reference docs.

## Maintaining these docs

Update the relevant reference document in the same change as the behaviour:

- New or changed route, request field or response shape → [API.md](API.md).
- New or changed user-visible workflow → [USER_GUIDE.md](USER_GUIDE.md).
- New limitation or removed workaround → [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).
- New environment variable or changed default → [deployment/ENVIRONMENT_VARIABLES.md](deployment/ENVIRONMENT_VARIABLES.md) and `.env.example`.
- Changed install or verification commands → [README.md](../README.md), [LOCAL_DEVELOPMENT.md](deployment/LOCAL_DEVELOPMENT.md) and [CONTRIBUTING.md](../CONTRIBUTING.md).
