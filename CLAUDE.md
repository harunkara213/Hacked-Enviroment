# Hacker Environment: guide for AI coding agents (Claude, Codex) and teammates

Litter cleanup app for HackED Beta 2026. Users report dirty spots with photos, volunteers clean
them, a random nearby person confirms the cleanup in person, then points are paid.
Live: https://hacker-environment.onrender.com. **Every push to `main` deploys to the live site.**

## Workflow rules
- **Never push to `main` directly.** Start from the latest main (`git checkout main && git pull`),
  work on a branch (`git checkout -b feature/short-name`), push it and open a Pull Request.
- **Run the checks before every push:** `.venv/bin/python tests/run_checks.py` must print `ALL PASSED`.
  Add a check for every new rule you add.
- Small, focused changes. Don't reformat, rename or rewrite code you aren't changing.
- Never commit secrets. `GEMINI_API_KEY` lives in `.env` locally and in Render's environment.
- Set your git name once: `git config user.name "<your GitHub username>"`.

## Stack (keep it)
- `app.py`: FastAPI, every endpoint and game rule. `ai.py`: Gemini calls (photo scoring,
  cleanup check, fake check). `db.py`: database layer. `photocheck.py`: photo fingerprints.
- `static/index.html` + `static/app.js`: plain JS, Tailwind CDN, Leaflet. No build step, no npm.
- Mobile-first: every screen must work at 375 px wide.

## Database: works on SQLite (local) AND Postgres (Render). Keep both working
- Locally (no `DATABASE_URL`) the app uses `data.db`. On Render it uses Postgres. Render's web
  server disk is wiped on every deploy and sleep, so **never store anything on disk**, including photos.
- Write SQLite-style SQL with `?` placeholders; `db.py` adapts it for Postgres.
- Don't use `SUM(condition)`; write `SUM(CASE WHEN condition THEN 1 ELSE 0 END)`.
- `user` is a reserved word in Postgres; `db.py` quotes it automatically, so write it plainly.
- Use `INSERT ... ON CONFLICT DO NOTHING`, never `INSERT OR IGNORE`.
- New columns: add them to `SPOT_EXTRA_COLUMNS` (migrated on startup), never edit `CREATE TABLE` alone.
- Inside a `with closing(db()) as c, c:` block, pass `c=c` to `save_photo(...)`, or SQLite locks itself.

## Anti-cheat rules: do not weaken these
- Points are paid only after a **random nearby user** confirms the cleanup in person
  (not the reporter, not the cleaner). Disputes go to a second verifier.
- Every after/check photo must be within 250 m (GPS). A before photo is required to start a cleanup.
  GPS must also be precise enough: `MAX_ACTION_LOCATION_ACCURACY_M` (Render env, default 75 m) is the one
  setting for this; the app reads it from `/api/status`. Raise it if GPS is weak indoors at a demo.
- Cleaning your own report or "I'll clean it myself" = solo cleanup: half points, report bonus
  taken back, max 3 a day, 60 s minimum, always checked in person, never counts toward donations.
- AI fake check (screens, screenshots, stock, AI-made) = strike. Any strike or 2+ complaints locks
  donations for good. Donations: $1-20, $50/month cap, 7-day hold, go to a person, not a cleanup.
- **Photos must be new**: the app rejects gallery uploads, photos older than 10 minutes, PNG
  screenshots and files whose metadata says AI-generated/AI-edited (IPTC/C2PA digital source type
  "trainedAlgorithmicMedia", case-insensitive) or edited (Photoshop...). A C2PA label by itself is
  NOT a reason to reject: Pixel/Galaxy/iPhone cameras add it to normal photos. The server rejects
  any photo that was already uploaded (`photocheck.py`). App-side rejections are reported to
  `/api/client-reject` and the last 5 show on `/api/status` (check there for false alarms).
- If the AI fails, scoring must not accept photos automatically (`ai.py` demo path gives 0 points).

## AI
- Default model in `ai.py` (`MODEL`). If it doesn't exist for our key, `_generate` falls back to
  `gemini-2.5-flash`. `/api/status` shows the model actually in use and the last AI error.
- Keep structured JSON output (`response_schema`) and the `looks_fake` field in every schema.

## Run locally
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # add GEMINI_API_KEY, or leave empty for demo mode
.venv/bin/uvicorn app:app --reload --port 8000
.venv/bin/python tests/run_checks.py
```
