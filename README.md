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
4. **Clean and AI pre-check**: take an after photo on site. AI compares it with the report and task-start photos. If it passes, points go **on hold**.
5. **In-person check**: a different volunteer near the spot is picked to go look. Their confirmation releases the points, and they earn +10 for checking.
6. **Compete**: leaderboard, levels (Seedling → Planet Hero) and a shareable score card.

### Anti-cheat
- You can't clean or verify a spot you reported, and you can't verify your own cleanup.
- Every after photo and check photo must be taken within 250 m of the spot.
- A task before photo is required within 250 m before cleaning can start.
- AI must confirm it's the same place and at least 60% cleaned before a human is even asked.
- The verifier is picked at random, so you can't send a friend. If nobody checks within 6 h, any nearby user can.
- If a verifier says "dirty" but the AI sees it clean, a second verifier is asked instead of punishing the cleaner.
- Anyone passing by can flag a cleaned spot as still dirty: the cleaner loses the points and gets a strike.
- **Trust score:** after 3 confirmed cleanups with no strikes, only 1 in 5 cleanups gets an in-person check.
- Nobody verifies within 48 h: the cleaner gets half the points from the AI check alone.
- Points are capped per user per day.
- AI checks every photo for fakes (photo of a screen, screenshot, stock or AI image). A fake photo is a strike.

### Solo cleanups ("I found it, I'll clean it myself")
Found a mess and want to clean it right away? Tap **I'll clean it myself** on the Report screen. Your report photos become the before photos and the spot is yours to clean. To keep "litter, then clean it for points" from paying off:
- Half points, no report bonus, and at most 3 solo cleanups a day.
- The after photo can't come within 60 s of the before photo, and must be taken at the spot.
- Always checked in person by a random nearby volunteer, even for trusted users.
- Solo cleanups don't count toward unlocking donations.

### Tasks
The Tasks tab lists open spots sorted by **nearest** or **most points**, plus any cleanup you were picked to check.

### Donations (only for proven, trusted volunteers)
People can tip a volunteer, but the rules are built so littering for money never pays:
- **Unlock only after:** 10 cleanups confirmed *in person* (AI-only and skipped checks don't count), checked by 5+ different people, on spots reported by 5+ different people, and an account at least 14 days old.
- **Locked for good** after any failed check, "still dirty" flag or fake photo, or complaints from 2+ different users. Brand-new accounts can't file complaints.
- Donations go to the **person, not a specific cleanup**, so there's no per-spot payout to farm.
- **$1–$20 per donation, max $50 per month** per volunteer.
- Every donation is **held 7 days** and refunded automatically if the volunteer gets locked during that time.
- Donations are recorded only for now. Real payouts would go through Stripe Connect with the same hold.
- For a demo, set `DONATION_DEMO=1` to lower the unlock bar to 2 confirmed cleanups by 2 different people.

## Tech

- **Backend:** Python, FastAPI, SQLite
- **AI:** Google Gemini (`google-genai`) with structured JSON output
- **Frontend:** HTML, Tailwind CSS, vanilla JS, Leaflet + OpenStreetMap
- Phone camera and GPS through the browser, installable as a PWA

If the AI is unavailable (no key, no network), the app falls back to a demo score so it keeps working.

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

## What's next

- Donations unlocked only for trusted, verified volunteers
- Businesses and cities sponsoring point bounties on areas
- Group cleanup events and team leaderboards
- Native app wrapper (Capacitor) with push notifications for nearby spots
