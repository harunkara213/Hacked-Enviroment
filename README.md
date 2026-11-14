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
- Every before, after and check photo must be taken at the spot: within 400 m by GPS, with the phone's reported
  accuracy counted in the user's favour (indoor Wi-Fi positioning is often hundreds of metres off). If it's too far,
  the app keeps the photo and offers "Try again" with tips (step outside, turn on Precise Location).
- AI must confirm it's the same place and at least 60% cleaned before any points are paid.
- Anyone passing by can flag a cleaned spot as still dirty and it goes back on the map. Some places get dirty
  again fast, so a cleaner's first 2 flags cost nothing; from the 3rd on, the points are taken back and it's a strike.
- Points are capped per user per day.
- **Photo authenticity checks:**
  - ON: nothing older than 10 minutes and no screenshots (PNG);
  - ON: a perceptual fingerprint that refuses photos already uploaded (even re-saved or resized);
  - OFF for now: files whose metadata marks them as AI-generated or AI-edited (IPTC / C2PA "digital source type").
  Switches: `PHOTO_CHECKS` in `static/app.js`, `CHECK_REUSED_PHOTOS=1` on the server.

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
- **Locked for good** after a strike (a "still dirty" flag beyond the first 2 free ones), or after complaints filed internally by the team (complaints are never shown in the app).
- Publicly, only **"💛 Verified · donations on"** is shown. Nobody else's donation status is visible to other users.
- Donations go to the **person, not a specific cleanup**, so there's no per-spot payout to farm.
- **Promises:** a verified volunteer can say "I'll remove 100 kg of trash if I get $50". Their profile and Ranks
  show two progress bars: donations raised and kg removed since the promise (~5 kg per bag).
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
- Paying for itself: local sponsors on spot cards, businesses and cities funding point bounties, and small ads.
  AI costs about $0.002 per photo (Gemini 2.5 Flash, thinking off), about $0.005 per report-and-clean cycle.
- Group cleanup events and team leaderboards
- Native app wrapper (Capacitor) with push notifications for nearby spots
