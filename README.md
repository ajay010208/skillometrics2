# SkilloMetrics — Unified Python Backend (prototype)

One FastAPI process replacing the Express API **and** the FastAPI AI service of
[skillometrics](https://github.com/ajay010208/skillometrics). Built to compare the
two architectures on real data — see [COMPARISON.md](COMPARISON.md) for the verdict.

## Why

The original stack runs two services in two languages (Node :4000 + Python :8000),
so every AI feature pays an HTTP hop and the chat agent needs ~50 lines of JSON
plumbing per call. This prototype collapses both into one Python codebase where
AI calls are plain function calls and agent context is a direct DB read.

## What it does

- **Endpoint-compatible** with the Express API for the demo path (13 routes:
  auth, skill-analysis, roadmap build/get, assessments start/submit, job matches,
  market insights, chat) — same JSON shapes, so the existing web app works by
  changing one base URL.
- **Same math, verified**: parity-checked against the live Express API on the same
  database — identical readiness (68), estimated weeks (7), jobs analyzed (19),
  requirement gaps (2/6, same top-3).
- **AI inline with fallbacks**: quiz generation and the career-counsellor agent
  run in-process; without an `AI_API_KEY` they use deterministic fallbacks so the
  demo never breaks.
- **Reads the original Prisma `dev.db`** — SQLAlchemy models mirror the existing
  tables/columns; both backends can run side-by-side on one dataset.

## Run

```bash
pip install -r requirements.txt
python main.py                      # 127.0.0.1:5001
# optional env: UNIFIED_DB=../apps/api/prisma/dev.db  UNIFIED_PORT=5001
#               AI_API_KEY / AI_MODEL for real LLM mode
```

Or just open the browser console and click through the demo path:

```
http://localhost:5001/console
```

Self-contained page (no CDN, no build): pick a persona, sign in, then fire
`skill-analysis`, `roadmap`, `matches`, `market/insights`, or start an
assessment — real JSON from the real dev.db, with status codes and latency.

### curl equivalent

```bash
curl localhost:5001/api/auth/personas
curl -X POST localhost:5001/api/auth/demo-login \
     -H 'Content-Type: application/json' \
     -d '{"email":"demo.trainee@skillometrics.in"}'
# then use the returned profile id as:  -H "x-demo-token: <id>"
```

## Run with the real frontend (combined mode)

The React app works against this backend unchanged — its Vite proxy target is
configurable in the main repo:

```bash
# terminal 1
cd unified-backend && python main.py          # :5001

# terminal 2 (main repo) — point the web app at the Python backend
API_TARGET=http://localhost:5001 npm run dev -w web
```

Open the app (default :5173, or `WEB_PORT=5002` to run both stacks side by
side), log in with a demo persona, and the whole trainee path — skill
analysis with the radar chart, Reality Check, roadmap, assessments — is
served by the single FastAPI process.

## Status

Prototype (~880 lines, demo path). Not ported: recruiter/provider/admin
dashboards, placements, follow-ups, reviews. The comparison and migration-cost
notes are in [COMPARISON.md](COMPARISON.md).
