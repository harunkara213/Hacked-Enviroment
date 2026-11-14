# Hacker Environment: guide for AI coding agents (Claude, Codex) and teammates

Litter cleanup app for HackED Beta 2026. Users report dirty spots with photos, volunteers clean
them, and the AI checks the before/after photos and pays the points. Anyone can flag a
"cleaned" spot that's still dirty, which takes the points back.
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
  before/after cleanup check). `db.py`: database layer. `photocheck.py`: photo fingerprints.
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
- Points are paid when the AI approves the after photo: same place as the before photos and at
  least 60% cleaned (`MIN_PERCENT_CLEANED`). There is no in-person verification step (removed on purpose).
- A "still dirty" flag (photo at the spot, AI must see litter) reopens the spot. A cleaner's first
  `FREE_FLAGS` (2) flags cost nothing, since some places get dirty again fast; from the next one on, the
  points are taken back and it's a strike.
- Verified volunteers can set one donation goal ("I'll remove X kg if I get $Y", $5-50, 10-1000 kg);
  progress = donations and ~5 kg per bag cleaned since the goal was set (`goal_of`).
- Every before/after/check photo must be at the spot: `MAX_CLEAN_DISTANCE_M` (default 400 m), minus the phone's
  reported accuracy (capped at `MAX_ACTION_LOCATION_ACCURACY_M`) as a margin, since indoor Wi-Fi GPS is often
  hundreds of metres off. A "too far" 403 carries `too_far: true`; the app keeps the photo and offers a retry.
  A before photo is required to start a cleanup.
  GPS must also be precise enough: `MAX_ACTION_LOCATION_ACCURACY_M` (Render env, default 75 m) is the one
  setting for this; the app reads it from `/api/status`. Raise it if GPS is weak indoors at a demo.
- Cleaning your own report or "I'll clean it myself" = solo cleanup: half points, report bonus
  taken back, max 3 a day, 60 s minimum, never counts toward donations.
- Any strike ("still dirty" flag) or 2+ complaints locks donations for good. Complaints are INTERNAL:
  no "report user" button, never shown in the app or API (`public_donation_status` strips them), filed only
  through `/api/users/{name}/complain` with the `TEAM_KEY` (Render env; without it the endpoint is closed). Publicly, only "💛 Verified · donations on" is shown;
  nobody else's donation status (locked or not) is visible to other users. Donations: $1-20, $50/month cap, 7-day hold, go to a person, not a cleanup.
- **Photo authenticity checks:** `maxAge` and `noScreenshots` are ON, `aiMetadata` is OFF (it flagged real
  iPhone photos), in `PHOTO_CHECKS` in `static/app.js`. Server side: `CHECK_REUSED_PHOTOS` (default 1 = ON). The app rejects photos older than
  10 minutes and PNG screenshots; with `aiMetadata` on it would also reject files whose metadata says AI-generated/AI-edited (IPTC/C2PA digital source type
  "trainedAlgorithmicMedia", case-insensitive) or edited (Photoshop...). A C2PA label by itself is
  NOT a reason to reject: Pixel/Galaxy/iPhone cameras add it to normal photos. The server rejects
  any photo that was already uploaded (`photocheck.py`). App-side rejections are reported to
  `/api/client-reject` and the last 5 show on `/api/status` (check there for false alarms).
- If the AI fails, scoring must not accept photos automatically: the `ai.py` demo path gives 0 points for a report
  and refuses a cleanup (same_place false); `/clean` answers 503 "try again" and the spot stays claimed.

## AI
- Default model in `ai.py` (`MODEL`, env `GEMINI_MODEL`): `gemini-2.5-flash` with thinking off (cost ~$0.002 per photo;
  3.8 Flash was ~5x more). Don't change it without checking the price. If it doesn't exist for our key, `_generate` falls back to
  `gemini-2.5-flash`. `/api/status` shows the model actually in use and the last AI error.
- Keep structured JSON output (`response_schema`). There is no AI fake-photo check (removed on purpose);
  fake photos are caught by the app-side metadata checks and photo fingerprints.

## Run locally
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # add GEMINI_API_KEY, or leave empty for demo mode
.venv/bin/uvicorn app:app --reload --port 8000
.venv/bin/python tests/run_checks.py
```
