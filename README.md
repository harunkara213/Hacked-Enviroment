# Hacker Environment

Spot trash. Clean it. Earn your **Generation Score**.

A mobile web app (installable to your home screen) where anyone can report a dirty spot
with a photo, AI estimates how hard it is to clean, and volunteers earn points by cleaning
it up and proving it with an "after" photo.

Built at HackED Beta 2026.

## How it works

1. **Report**: take 1–4 photos of litter. The app grabs your GPS location and pins it on the map.
2. **Score**: Gemini vision estimates difficulty (1–5), garbage bags needed, minutes and hazards. That becomes the spot's point value.
3. **Claim and clean**: a volunteer claims the spot, cleans it, and takes an after photo on site.
4. **Verify**: AI compares before and after (same place? how much is gone?) and awards points.
5. **Compete**: leaderboard, levels (Seedling → Planet Hero) and a shareable score card.

### Anti-cheat
- You can't clean a spot you reported.
- The after photo must be taken within 250 m of the spot.
- AI must confirm it's the same place and at least 60% cleaned.
- Points are capped per user per day.
- No cash rewards, so there's no incentive to litter on purpose.

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
