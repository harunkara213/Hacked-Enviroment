# Hacker Environment

Spot trash. Clean it. Earn your **Generation Score**.

A mobile web app (installable to your home screen) where anyone can report a dirty spot
with a photo, AI estimates how hard it is to clean, and volunteers earn points by cleaning
it up and proving it with an "after" photo.

Built at HackED Beta 2026.

## How it works

1. **Report**: take 1–4 photos of litter. The app grabs your GPS location and pins it on the map.
2. **Score**: Gemini vision estimates difficulty (1–5), garbage bags needed, minutes and hazards. That becomes the spot's point value.
3. **Find and start**: after sharing location, volunteers can see the closest open cleanup. Claim it and take an on-site before photo to start.
4. **Clean and AI check**: take an after photo on site. AI compares it with the report and before photos; if it's the same place and clean, the points are paid.
5. **Compete**: leaderboard, levels (Seedling → Planet Hero) and a shareable score card.

### Anti-cheat
- You can clean a spot you reported, but it counts as a solo cleanup (half points, report bonus taken back, max 3 a day, 60 s minimum).
- Every after photo and check photo must be taken within 250 m of the spot.
- A task before photo is required within 250 m before cleaning can start.
- AI must confirm it's the same place and at least 60% cleaned before any points are paid.
- Anyone passing by can flag a cleaned spot as still dirty: the cleaner loses the points and gets a strike.
- Points are capped per user per day.
- Photos must be taken now with the camera: no gallery uploads, nothing older than 10 minutes, no screenshots,
  and files whose metadata marks them as AI-generated or AI-edited (the IPTC / C2PA "digital source type") or
  edited in Photoshop and other editors are refused. (A Content Credentials label alone is fine: many phones add
  one to every normal camera photo.)
- Every photo gets a perceptual fingerprint. A photo that was already uploaded (even re-saved or resized) is
  refused, which blocks reusing the before photo as the after photo and one photo for several spots.

### Photo authenticity: what's next when we grow
No method proves a photo is real with 100% certainty, so the app stacks checks and keeps a human in the loop
("still dirty" flags, internal review). With funding we'd add:
- An in-app camera with signed captures (iOS App Attest / Android Play Integrity) so photos can only come
  from the real camera of a real device.
- A dedicated AI-image detector API (e.g. Hive, Sightengine, Reality Defender) plus Google SynthID detection.
- Reverse image search (Google Cloud Vision web detection) to catch photos taken from the internet.
- Full C2PA signature verification on the server.

### Solo cleanups ("I found it, I'll clean it myself")
Found a mess and want to clean it right away? Tap **I'll clean it myself** on the Report screen. Your report photos become the before photos and the spot is yours to clean. To keep "litter, then clean it for points" from paying off:
- Half points, no report bonus, and at most 3 solo cleanups a day.
- The after photo can't come within 60 s of the before photo, and must be taken at the spot.
- Solo cleanups don't count toward unlocking donations.

### Tasks
The Tasks tab lists open spots sorted by **nearest** or **most points**, plus the spot you're cleaning.

### Donations (only for proven, trusted volunteers)
People can tip a volunteer, but the rules are built so littering for money never pays:
- **Unlock only after:** 10 approved cleanups of other people's reports (solo cleanups don't count), on spots reported by 5+ different people, and an account at least 14 days old.
- **Locked for good** after any "still dirty" flag, or after complaints reviewed internally by the team (complaints are never shown in the app).
- Publicly, only **"💛 Verified · donations on"** is shown. Nobody else's donation status is visible to other users.
- Donations go to the **person, not a specific cleanup**, so there's no per-spot payout to farm.
- **$1–$20 per donation, max $50 per month** per volunteer.
- Every donation is **held 7 days** and refunded automatically if the volunteer gets locked during that time.
- Donations are recorded only for now. Real payouts would go through Stripe Connect with the same hold.
- For a demo, set `DONATION_DEMO=1` to lower the unlock bar to 2 approved cleanups of 2 different people's reports.

## Tech

- **Backend:** Python, FastAPI, PostgreSQL (Render) / SQLite (local)
- **AI:** Google Gemini (`google-genai`) with structured JSON output
- **Frontend:** HTML, Tailwind CSS, vanilla JS, Leaflet + OpenStreetMap
- Phone camera and GPS through the browser, installable as a PWA

If the AI is unavailable (no key, no network), photos are never accepted automatically (0 points).

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # add your GEMINI_API_KEY
.venv/bin/uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Open http://localhost:8000. Phones need HTTPS for the camera and GPS, so deploy it (below) to test on a phone.

## Deploy (Render)

Push to GitHub, create a new Render **Blueprint** from the repo (it reads `render.yaml`), and set `GEMINI_API_KEY`.

Data (spots, points, photos) lives in a free Render **Postgres** database that the Blueprint creates and connects through `DATABASE_URL`. Render's free web server wipes its own disk on every deploy and sleep, so nothing is stored there. Locally, without `DATABASE_URL`, the app uses a SQLite file (`data.db`) instead. Note: Render's free Postgres expires after 30 days.

## What's next

- Donations unlocked only for trusted, verified volunteers
- Businesses and cities sponsoring point bounties on areas
- Group cleanup events and team leaderboards
- Native app wrapper (Capacitor) with push notifications for nearby spots
