# Unified Python Backend — Architecture Comparison

Prototype at `unified-backend/main.py`: one FastAPI process replacing both the
Express API (`apps/api`) and the FastAPI AI service (`apps/ai`). It runs against
the **same `dev.db`** on port **5001** so both architectures can be compared live.

## Side-by-side

| Dimension | Current: Express + FastAPI (2 services) | Prototype: unified FastAPI (1 service) |
|---|---|---|
| Processes | 2 (Node :4000, Python :8000) | 1 (:5001) |
| Languages | TypeScript + Python | Python only |
| AI call path | HTTP POST + `{data, source}` envelope unwrap + AbortController timeout | **plain `async` function call** |
| Agent context (chat) | ~50 lines assembling JSON in chat.ts, shipped over HTTP | direct DB reads in the same function |
| Auth | custom JWKS verify in node:crypto (~60 lines) | PyJWT `PyJWK` (~25 lines) |
| DB access | Prisma (schema-first, type-safe client) | SQLAlchemy 2.0 (models mirror the same tables) |
| Deploy surface | 2 runtimes, 2 Dockerfiles, 2 health checks | 1 container, 1 health check |
| Team skill needed | JS + Python | Python only |
| Failure modes | cross-service timeouts, port conflicts, envelope drift | none of these; single process to monitor |

## What the prototype ports (demo path, endpoint-compatible)

`/health`, `/api/auth/oauth-configured`, `/api/auth/personas`, `/api/auth/demo-login`,
`/api/auth/me`, `/api/skill-analysis`, `/api/roadmap`, `/api/roadmap/build`,
`/api/assessments/start`, `/api/assessments/:id/submit`, `/api/jobs/matches`,
`/api/market/insights`, `/api/chat` — same JSON shapes as the Express API, so the
existing web app works against it by changing one base URL.

Not yet ported (returns 404): recruiter/provider/admin dashboards, placements,
follow-ups, reviews, resources listing. The point is the architecture, not parity.

## How to run the comparison

```powershell
# 1) real stack (already running): Express :4000 + FastAPI :8000
# 2) prototype:
cd unified-backend
python -m venv .venv; .venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py        # :5001, same dev.db, same seed data

# compare the same endpoint on both:
curl http://localhost:4000/api/auth/personas -H "x-demo-token: <id>"
curl http://localhost:5001/api/auth/personas -H "x-demo-token: <id>"
```

## Honest verdict

**Unify if:** the product stays AI-heavy, the team is Python-comfortable, you want
one deployable. The AI service stops being a "service" — it becomes a module, and
features like the chat agent stop paying a network tax per tool call.

**Keep the split if:** Node skills dominate the team, or you foresee scaling the
AI service independently (different CPU/GPU needs, separate rate limits) — the
current split does make that boundary explicit.

**Migration reality check (the part slide decks skip):** the unified prototype is
~850 lines for the demo path; the Express API is ~2,800 lines total. Full parity is
a multi-day port with real risk in the long tail (placements, follow-ups, admin
CSV logic) — Prisma's generated types are also genuinely lossy to translate, and
the web app's TypeScript API layer would need re-typeing against new shapes.
