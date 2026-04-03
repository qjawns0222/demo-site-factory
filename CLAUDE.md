# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI Demo Site Factory — a full-stack web app that lets users enter a domain topic, then streams AI-generated documentation through a multi-step workflow. Each step's output can be edited, approved, and synthesized into context for the next step. Results are exportable as a ZIP archive.

## Running the Project

### Full stack (recommended)
```bash
# Create .env with your Gemini key first
echo "GEMINI_API_KEY=your_key_here" > .env

docker-compose up --build
# Frontend: http://localhost:3000
# Backend:  http://localhost:8000
```

### Backend only (local dev)
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```
Requires Redis running at `redis://localhost:6379`. Without a `GEMINI_API_KEY`, the backend enters **DUMMY MODE** and returns fake streamed responses.

### Frontend only (local dev)
```bash
cd frontend
npm install
npm run dev        # http://localhost:3000
npm run build      # production build
npm run lint       # ESLint check
```
Set `NEXT_PUBLIC_API_URL=http://localhost:8000` (defaults to this value in code).

## Architecture

### Backend (`backend/`)
- **`main.py`** — FastAPI app (single file). Manages sessions via UUID, streams Gemini responses over SSE, exports ZIP archives.
- **`data_init.py`** — On startup, seeds `backend/data/prompts.db` (SQLite) from `backend/prompts/step_N.txt` files (only if the DB table is empty).
- **`backend/prompts/step_1.txt` … `step_9.txt`** — The LLM prompt for each workflow step. Edit these to change what Gemini is asked to generate.

Key backend dependencies: Redis (session/context storage, TTL 24h), SQLite (prompt storage), Gemini API (`gemini-1.5-pro-latest`), slowapi (rate limiting: 10 req/min on `/api/generate`), tenacity (5-attempt retry with exponential backoff on JSON parse failures).

### Frontend (`frontend/src/`)
- **`store/useWorkflowStore.ts`** — Zustand store: single source of truth for `domain`, `sessionId`, `steps[]`, and streaming state.
- **`components/Header.tsx`** — Domain input, Boot System (POST `/api/generate`), Session Reset (DELETE `/api/session/:id`), ZIP Export.
- **`components/Sidebar.tsx`** — Workflow step list. Clicking a step opens an SSE connection to `GET /api/stream_step/:sessionId/:stepId` and appends streamed chunks to the store.
- **`components/Workspace.tsx`** — Displays selected step content as rendered Markdown or raw editable textarea. "Approve & Close" button POST `/api/step/:sessionId/:stepId/synthesize` to build context for the next step.
- **`app/page.tsx`** — Root layout: `<Header> | <Sidebar> | <Workspace>`.

### Key API Flow
1. `POST /api/generate` → returns `session_id` (UUID), stores domain in Redis.
2. `GET /api/stream_step/:sessionId/:stepId` → SSE stream of Gemini tokens. Emits `chunk`, `completed`, `finished`, `error` events.
3. `POST /api/step/:sessionId/:stepId/synthesize` → summarizes approved content via Gemini, stores JSON context in Redis for subsequent steps.
4. `GET /api/export/:sessionId` → streams a ZIP of all step Markdown + extracted code blocks.

## Prompt Management

To modify what AI generates for each step, edit `backend/prompts/step_N.txt`. Changes take effect on next container start **only if the SQLite DB is empty** (first run). To force a re-seed, delete `backend/data/prompts.db`.

## Environment Variables

| Variable | Where | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | backend | Gemini API key; if unset, backend runs in dummy mode |
| `REDIS_URL` | backend | Redis URL (default: `redis://redis:6379`) |
| `NEXT_PUBLIC_API_URL` | frontend | Backend URL (default: `http://localhost:8000`) |

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
