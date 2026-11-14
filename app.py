"""Hacker Environment - report litter, clean it up, earn points.

Flow: report -> claim -> clean (AI pre-check, points ON HOLD) -> a random nearby
person checks it in person -> points paid. Anyone can later flag "still dirty".

Run:  .venv/bin/uvicorn app:app --reload --host 0.0.0.0 --port 8000
"""
import json
import math
import os
import random
import time
import uuid
from contextlib import closing
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import ai  # noqa: E402
import db as dbmod  # noqa: E402

BASE = Path(__file__).parent

MAX_PHOTOS = 4
MAX_PHOTO_BYTES = 8 * 1024 * 1024
DAILY_POINT_CAP = int(os.getenv("DAILY_POINT_CAP", "1000"))
MAX_CLEAN_DISTANCE_M = float(os.getenv("MAX_CLEAN_DISTANCE_M", "250"))
MIN_PERCENT_CLEANED = 60
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}

VERIFIER_RADIUS_M = 5000           # prefer verifiers who were seen this close to the spot
VERIFIER_WINDOW_S = 6 * 3600       # assigned verifier has this long before anyone nearby may verify
PENDING_TIMEOUT_S = 48 * 3600      # nobody verified -> cleaner gets half, from the AI check alone
VERIFY_REWARD = 10
FLAG_REWARD = 10
TRUSTED_MIN_CLEANUPS = 3           # after this many confirmed cleanups with no strikes...
TRUSTED_SKIP_RATE = float(os.getenv("TRUSTED_SKIP_RATE", "0.8"))  # ...only 20% get spot-checked

# Donations: only for proven, clean-record volunteers. Any strike or 2+ complaints = locked for good.
# DONATION_DEMO=1 lowers the bar so the flow can be shown at a hackathon.
_DEMO = os.getenv("DONATION_DEMO") == "1"
DON_MIN_VERIFIED = 2 if _DEMO else 10      # cleanups confirmed IN PERSON (not AI-only, not skipped)
DON_MIN_VERIFIERS = 2 if _DEMO else 5      # ...by this many different people (no friend rings)
DON_MIN_REPORTERS = 1 if _DEMO else 5      # ...on spots reported by this many different people
DON_MIN_DAYS = 0 if _DEMO else 14          # account age
DON_MAX_COMPLAINTS = 1                     # 2+ complaints from different users locks donations
DON_MONTHLY_CAP_CENTS = 5000               # max $50 per cleaner per month: not worth littering for
DON_HOLD_S = 7 * 24 * 3600                 # donations are held 7 days and refunded if a problem shows up
COMPLAINT_REASONS = {"fake_photos", "littering", "fake_cleanup", "harassment", "other"}
STRIKE_KINDS = ("rejected", "revoke", "fake")

# "I found it and I'm cleaning it myself": allowed, but made unattractive for anyone who'd litter first.
SELF_CLEAN_FACTOR = 0.5            # half points
SELF_CLEAN_DAILY_LIMIT = 3         # per user per 24h
SELF_CLEAN_MIN_SECONDS = 60        # before -> after can't be instant

LEVELS = [  # (min points, title)
    (0, "Seedling"),
    (100, "Sprout"),
    (300, "Sapling"),
    (700, "Tree"),
    (1500, "Forest Guardian"),
    (3000, "Planet Hero"),
]


# ---------- database ----------

def db():
    """Postgres on Render (DATABASE_URL), SQLite file locally. See db.py."""
    return dbmod.connect()


SPOT_EXTRA_COLUMNS = {
    "pending_points": "INTEGER", "verifier": "TEXT", "verifier_assigned_at": "REAL",
    "excluded": "TEXT", "verified_by": "TEXT", "verified_at": "REAL",
    "verify_photo": "TEXT", "verify_note": "TEXT",
    "task_before_photo": "TEXT",
    "self_clean": "INTEGER",
    "seeded": "INTEGER",
}


def init_db():
    with closing(db()) as c, c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS spots (
            id TEXT PRIMARY KEY,
            lat REAL NOT NULL, lng REAL NOT NULL,
            reporter TEXT NOT NULL,
            photos TEXT NOT NULL,
            difficulty INTEGER, bags INTEGER, minutes INTEGER,
            hazards TEXT, summary TEXT, points INTEGER, ai_source TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            claimed_by TEXT, claimed_at REAL,
            cleaner TEXT, after_photo TEXT, verify_comment TEXT,
            percent_cleaned INTEGER, awarded INTEGER, cleaned_at REAL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            user TEXT NOT NULL, kind TEXT NOT NULL, points INTEGER NOT NULL,
            spot_id TEXT, ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            name TEXT PRIMARY KEY, lat REAL, lng REAL, seen REAL
        );
        CREATE TABLE IF NOT EXISTS complaints (
            from_user TEXT NOT NULL, about_user TEXT NOT NULL, reason TEXT NOT NULL,
            spot_id TEXT, ts REAL NOT NULL, UNIQUE(from_user, about_user)
        );
        CREATE TABLE IF NOT EXISTS donations (
            id TEXT PRIMARY KEY, donor TEXT NOT NULL, cleaner TEXT NOT NULL,
            amount_cents INTEGER NOT NULL, ts REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'held', release_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS photos (
            name TEXT PRIMARY KEY, mime TEXT NOT NULL, data BLOB NOT NULL
        );
        """)
        have = c.columns("spots")
        for col, typ in SPOT_EXTRA_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE spots ADD COLUMN {col} {typ}")


init_db()


# ---------- helpers ----------

def clean_name(name: str) -> str:
    name = (name or "").strip()[:24]
    if not name:
        raise HTTPException(400, "Pick a username first.")
    return name


def distance_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def require_on_site(spot, lat, lng, what="photo"):
    dist = distance_m(lat, lng, spot["lat"], spot["lng"])
    if dist > MAX_CLEAN_DISTANCE_M:
        raise HTTPException(403, f"You're {int(dist)} m away. Take the {what} at the spot.")


async def read_photo(f: UploadFile) -> tuple[bytes, str]:
    mime = (f.content_type or "").lower()
    if mime not in ALLOWED_MIME:
        raise HTTPException(400, f"Unsupported image type: {mime or 'unknown'}")
    data = await f.read()
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(400, "Photo is empty or too large (max 8 MB).")
    return data, mime


def save_photo(data: bytes, mime: str, c=None) -> str:
    """Photos live in the database (not on disk) so they survive Render restarts.
    Pass `c` when already inside a write transaction, so SQLite doesn't lock itself."""
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime]
    name = uuid.uuid4().hex + ext
    if c is not None:
        c.execute("INSERT INTO photos (name, mime, data) VALUES (?,?,?)", (name, mime, data))
    else:
        with closing(db()) as c2, c2:
            c2.execute("INSERT INTO photos (name, mime, data) VALUES (?,?,?)", (name, mime, data))
    return name


def load_photo(name: str) -> tuple[bytes, str]:
    with closing(db()) as c:
        r = c.execute("SELECT data, mime FROM photos WHERE name=?", (name,)).fetchone()
    if not r:
        raise HTTPException(410, "A photo for this spot is missing. Please report the spot again.")
    return bytes(r["data"]), r["mime"]


def spot_out(r) -> dict:
    d = dict(r)
    d["photos"] = ["/uploads/" + p for p in json.loads(d["photos"])]
    d["hazards"] = json.loads(d["hazards"] or "[]")
    d["excluded"] = json.loads(d["excluded"] or "[]")
    for k in ("after_photo", "verify_photo"):
        if d[k]:
            d[k] = "/uploads/" + d[k]
    if d.get("task_before_photo"):
        d["task_before_photo"] = "/uploads/" + d["task_before_photo"]
    d["open_to_anyone"] = d["status"] == "pending" and (
        d["verifier"] is None or time.time() - (d["verifier_assigned_at"] or 0) > VERIFIER_WINDOW_S)
    return d


def get_spot(c, spot_id):
    s = c.execute("SELECT * FROM spots WHERE id=?", (spot_id,)).fetchone()
    if not s:
        raise HTTPException(404, "Spot not found.")
    return s


def seen(c, user, lat=None, lng=None):
    """Remember where users were last seen, so verifiers can be picked nearby."""
    if lat is None:
        c.execute("INSERT INTO users (name, seen) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET seen=excluded.seen",
                  (user, time.time()))
    else:
        c.execute("""INSERT INTO users (name, lat, lng, seen) VALUES (?,?,?,?)
                     ON CONFLICT(name) DO UPDATE SET lat=excluded.lat, lng=excluded.lng, seen=excluded.seen""",
                  (user, lat, lng, time.time()))


def points_today(c, user: str) -> int:
    since = time.time() - 24 * 3600
    row = c.execute("SELECT COALESCE(SUM(points),0) FROM events WHERE user=? AND ts>? AND points>0",
                    (user, since)).fetchone()
    return row[0]


def award(c, user, points, kind, spot_id) -> int:
    """Insert a points event. Positive points respect the daily cap."""
    if points > 0:
        points = max(0, min(points, DAILY_POINT_CAP - points_today(c, user)))
    c.execute("INSERT INTO events VALUES (?,?,?,?,?)", (user, kind, points, spot_id, time.time()))
    return points


def trust_of(c, user) -> dict:
    r = c.execute("""SELECT COALESCE(SUM(CASE WHEN kind='clean' THEN 1 ELSE 0 END),0) AS cleanups,
                            COALESCE(SUM(CASE WHEN kind IN ('rejected','revoke','fake') THEN 1 ELSE 0 END),0) AS strikes
                     FROM events WHERE user=?""", (user,)).fetchone()
    trusted = r["cleanups"] >= TRUSTED_MIN_CLEANUPS and r["strikes"] == 0
    return {"cleanups": r["cleanups"], "strikes": r["strikes"], "trusted": trusted}


def strike_fake(user, spot_id, who="Your"):
    """AI thinks a photo is a screen/screenshot/AI image: record a strike and refuse."""
    with closing(db()) as c, c:
        award(c, user, 0, "fake", spot_id)
    raise HTTPException(422, f"{who} photo looks fake (a screen, screenshot or edited image). "
                             "This counts as a strike. Take a real photo at the spot.")


def donation_status(c, user) -> dict:
    """Every rule must pass. Problems (strikes, complaints) lock donations permanently."""
    t = trust_of(c, user)
    v = c.execute("""SELECT COUNT(*) AS n, COUNT(DISTINCT verified_by) AS verifiers,
                            COUNT(DISTINCT reporter) AS reporters
                     FROM spots WHERE cleaner=? AND status='cleaned' AND verified_by IS NOT NULL
                       AND reporter != cleaner""",
                  (user,)).fetchone()
    complaints = c.execute("SELECT COUNT(DISTINCT from_user) FROM complaints WHERE about_user=?",
                           (user,)).fetchone()[0]
    first = c.execute("SELECT MIN(ts) FROM events WHERE user=?", (user,)).fetchone()[0]
    days = (time.time() - first) / 86400 if first else 0
    month = c.execute("""SELECT COALESCE(SUM(amount_cents),0) FROM donations
                         WHERE cleaner=? AND status!='refunded' AND ts>?""",
                      (user, time.time() - 30 * 86400)).fetchone()[0]
    rules = [
        {"label": f"{DON_MIN_VERIFIED} cleanups confirmed in person", "have": v["n"], "need": DON_MIN_VERIFIED,
         "ok": v["n"] >= DON_MIN_VERIFIED},
        {"label": f"Checked by {DON_MIN_VERIFIERS}+ different people", "have": v["verifiers"],
         "need": DON_MIN_VERIFIERS, "ok": v["verifiers"] >= DON_MIN_VERIFIERS},
        {"label": f"Spots reported by {DON_MIN_REPORTERS}+ different people", "have": v["reporters"],
         "need": DON_MIN_REPORTERS, "ok": v["reporters"] >= DON_MIN_REPORTERS},
        {"label": f"Member for {DON_MIN_DAYS}+ days", "have": int(days), "need": DON_MIN_DAYS,
         "ok": days >= DON_MIN_DAYS},
        {"label": "No failed checks, flags or fake photos, ever", "have": t["strikes"], "need": 0,
         "ok": t["strikes"] == 0, "permanent": True},
        {"label": "No complaints from other users", "have": complaints, "need": 0,
         "ok": complaints <= DON_MAX_COMPLAINTS, "permanent": True},
    ]
    locked = t["strikes"] > 0 or complaints > DON_MAX_COMPLAINTS
    return {"eligible": all(r["ok"] for r in rules), "locked": locked, "rules": rules,
            "complaints": complaints, "month_cents": month,
            "room_cents": max(0, DON_MONTHLY_CAP_CENTS - month)}


def settle_donations(c, user):
    """Held donations: refund everything if the cleaner got locked, else release after the hold."""
    st = donation_status(c, user)
    if st["locked"]:
        c.execute("UPDATE donations SET status='refunded' WHERE cleaner=? AND status='held'", (user,))
    else:
        c.execute("UPDATE donations SET status='released' WHERE cleaner=? AND status='held' AND release_at<?",
                  (user, time.time()))


def pick_verifier(c, spot, exclude: set[str]):
    """Random user (not reporter/cleaner/excluded), preferring people seen near the spot."""
    week = time.time() - 7 * 24 * 3600
    rows = c.execute("SELECT name, lat, lng FROM users WHERE seen > ?", (week,)).fetchall()
    rows = [r for r in rows if r["name"] not in exclude]
    near = [r for r in rows if r["lat"] is not None
            and distance_m(r["lat"], r["lng"], spot["lat"], spot["lng"]) <= VERIFIER_RADIUS_M]
    pool = near or rows
    return random.choice(pool)["name"] if pool else None


def settle_expired(c):
    """Pending too long with nobody verifying: pay half, based on the AI check alone."""
    cutoff = time.time() - PENDING_TIMEOUT_S
    for s in c.execute("SELECT * FROM spots WHERE status='pending' AND cleaned_at < ?", (cutoff,)).fetchall():
        got = award(c, s["cleaner"], (s["pending_points"] or 0) // 2, "clean", s["id"])
        c.execute("""UPDATE spots SET status='cleaned', awarded=?, verified_by=?, verified_at=?,
                     verify_note=? WHERE id=?""",
                  (got, None, time.time(), "No one verified in 48h: half points from the AI check.", s["id"]))


def level_for(points: int) -> dict:
    title, floor, nxt = LEVELS[0][1], 0, None
    for i, (p, t) in enumerate(LEVELS):
        if points >= p:
            title, floor = t, p
            nxt = LEVELS[i + 1][0] if i + 1 < len(LEVELS) else None
    return {"title": title, "floor": floor, "next": nxt}


# ---------- demo data ----------
# A few park spots around U of A so the map isn't empty at the demo. Added once when
# SEED_DEMO=1 (never re-added after someone cleans or deletes them). Scores are set
# by hand: these are downloaded photos, which the AI's fake-photo check would reject.
DEMO_SPOTS = [
    {"id": "demo-emily-murphy", "lat": 53.5297, "lng": -113.5355, "file": "park-lawn-picnic.jpg",
     "reporter": "Ava", "difficulty": 3, "bags": 4, "minutes": 60, "hours_ago": 5,
     "hazards": ["Broken glass possible"],
     "summary": "Picnic litter scattered across the lawn: bags, bottles, cans and food wrappers."},
    {"id": "demo-windsor-playground", "lat": 53.5262, "lng": -113.5322, "file": "playground-benches.jpg",
     "reporter": "Leo", "difficulty": 1, "bags": 1, "minutes": 15, "hours_ago": 2,
     "hazards": [],
     "summary": "Spilled food, napkins and paper around the playground benches."},
    {"id": "demo-hawrelak-field", "lat": 53.5279, "lng": -113.5466, "file": "field-dump.jpg",
     "reporter": "Mia", "difficulty": 5, "bags": 10, "minutes": 180, "hours_ago": 20,
     "hazards": ["Sharp debris", "Heavy items"],
     "summary": "Large dump in the open field: plastic, paper and household waste spread over a wide area."},
]


def seed_demo():
    if os.getenv("SEED_DEMO") != "1":
        return
    with closing(db()) as c, c:
        for d in DEMO_SPOTS:
            if c.execute("SELECT 1 FROM spots WHERE id=?", (d["id"],)).fetchone():
                continue
            if c.execute("SELECT 1 FROM events WHERE spot_id=? AND kind='seed_removed'", (d["id"],)).fetchone():
                continue  # deleted on purpose: don't bring it back
            name = save_photo((BASE / "static" / "demo" / d["file"]).read_bytes(), "image/jpeg", c=c)
            c.execute("""INSERT INTO spots (id, lat, lng, reporter, photos, difficulty, bags, minutes,
                         hazards, summary, points, ai_source, created_at, seeded)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                      (d["id"], d["lat"], d["lng"], d["reporter"], json.dumps([name]), d["difficulty"],
                       d["bags"], d["minutes"], json.dumps(d["hazards"]), d["summary"],
                       ai.points_for(d["difficulty"], d["bags"]), "demo-seed",
                       time.time() - d["hours_ago"] * 3600))


seed_demo()


# ---------- app ----------

app = FastAPI(title="Hacker Environment")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/uploads/{name}")
def photo(name: str):
    with closing(db()) as c:
        r = c.execute("SELECT data, mime FROM photos WHERE name=?", (name,)).fetchone()
    if not r:
        raise HTTPException(404, "Photo not found.")
    # Names are random and never reused, so browsers can cache them forever.
    return Response(bytes(r["data"]), media_type=r["mime"],
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/manifest.json")
def manifest():
    return FileResponse(BASE / "static" / "manifest.json")


@app.get("/api/status")
def status():
    return {"ai": "gemini" if ai._get_client() else "demo", "model": ai.ACTIVE_MODEL, "last_error": ai.LAST_ERROR,
            "storage": "postgres" if dbmod.IS_PG else "sqlite (data is lost on restart)"}


@app.post("/api/ping")
def ping(user: str = Form(...), lat: float | None = Form(None), lng: float | None = Form(None)):
    """App open: remember the user (and where they are) so they can be picked as a verifier."""
    user = clean_name(user)
    with closing(db()) as c, c:
        seen(c, user, lat, lng)
    return {"ok": True}


@app.get("/api/spots")
def list_spots(lat: float | None = None, lng: float | None = None):
    with closing(db()) as c, c:
        settle_expired(c)
        rows = c.execute("SELECT * FROM spots ORDER BY created_at DESC").fetchall()
    result = [spot_out(r) for r in rows]
    if lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180:
        for spot in result:
            spot["distance_m"] = round(distance_m(lat, lng, spot["lat"], spot["lng"]))
        result.sort(key=lambda spot: (spot["status"] != "open", spot["distance_m"]))
    return result


@app.post("/api/spots")
async def report_spot(
    user: str = Form(...),
    lat: float = Form(...),
    lng: float = Form(...),
    photos: list[UploadFile] = File(...),
    self_clean: bool = Form(False),
):
    """Report a dirty spot. With self_clean=true the reporter cleans it themselves:
    these photos become the BEFORE photos and the spot is claimed by them right away."""
    user = clean_name(user)
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise HTTPException(400, "Invalid location.")
    if not 1 <= len(photos) <= MAX_PHOTOS:
        raise HTTPException(400, f"Add 1 to {MAX_PHOTOS} photos.")
    if self_clean:
        with closing(db()) as c:
            check_solo_limit(c, user)
    images = [await read_photo(p) for p in photos]

    result = ai.assess_spot(images)
    if result.get("looks_fake"):
        strike_fake(user, None)
    if not result["is_litter"]:
        if result.get("litter_confidence", 100) < 70:
            raise HTTPException(422, "I couldn't tell clearly from these photos. Move closer, use good lighting, and include the litter in focus.")
        raise HTTPException(422, "I couldn't confirm discarded litter in these photos. Try another angle that clearly shows the trash.")

    spot_id = uuid.uuid4().hex[:10]
    names = [save_photo(d, m) for d, m in images]
    points = round(result["points"] * SELF_CLEAN_FACTOR) if self_clean else result["points"]
    now = time.time()
    with closing(db()) as c, c:
        seen(c, user, lat, lng)
        c.execute("""INSERT INTO spots (id, lat, lng, reporter, photos, difficulty, bags, minutes,
                     hazards, summary, points, ai_source, created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (spot_id, lat, lng, user, json.dumps(names), result["difficulty"],
                   result["estimated_bags"], result["estimated_minutes"], json.dumps(result["hazards"]),
                   result["summary"], points, result["source"], now))
        if self_clean:
            c.execute("""UPDATE spots SET self_clean=1, status='claimed', claimed_by=?, claimed_at=?,
                         task_before_photo=? WHERE id=?""", (user, now, names[0], spot_id))
            bonus = 0
        else:
            bonus = award(c, user, 5, "report", spot_id)
        row = get_spot(c, spot_id)
    return {"spot": spot_out(row), "report_bonus": bonus}


def check_solo_limit(c, user):
    """Solo cleanups (reporting and cleaning your own spot) are capped per day, however they started."""
    n = c.execute("""SELECT COUNT(*) FROM spots WHERE reporter=? AND self_clean=1
                     AND COALESCE(claimed_at, created_at) > ?""",
                  (user, time.time() - 24 * 3600)).fetchone()[0]
    if n >= SELF_CLEAN_DAILY_LIMIT:
        raise HTTPException(429, f"You can clean {SELF_CLEAN_DAILY_LIMIT} of your own spots a day. "
                                 "Leave this one for another volunteer.")


@app.post("/api/spots/{spot_id}/release")
def release_spot(spot_id: str, user: str = Form(...)):
    """Give up a task you claimed: the spot goes back on the map at full points for anyone."""
    user = clean_name(user)
    with closing(db()) as c, c:
        s = get_spot(c, spot_id)
        if s["status"] != "claimed" or s["claimed_by"] != user:
            raise HTTPException(409, "You're not working on this spot.")
        tb = s["task_before_photo"]
        if tb and tb not in json.loads(s["photos"]):  # a solo report reuses its report photo: keep that
            c.execute("DELETE FROM photos WHERE name=?", (tb,))
        c.execute("""UPDATE spots SET status='open', claimed_by=NULL, claimed_at=NULL, task_before_photo=NULL,
                     self_clean=0, points=? WHERE id=?""", (ai.points_for(s["difficulty"], s["bags"]), spot_id))
        row = get_spot(c, spot_id)
    return spot_out(row)


@app.post("/api/spots/{spot_id}/delete")
def delete_spot(spot_id: str, user: str = Form(...)):
    """The reporter can take back a report while nobody is working on it. The +5 report bonus goes back too."""
    user = clean_name(user)
    with closing(db()) as c, c:
        s = get_spot(c, spot_id)
        if s["reporter"] != user:
            raise HTTPException(403, "Only the person who reported this spot can delete it.")
        if s["status"] != "open":
            raise HTTPException(409, "Someone is already cleaning or checking this spot, so it can't be deleted.")
        bonus = c.execute("""SELECT COALESCE(SUM(points),0) FROM events WHERE "user"=? AND spot_id=?
                             AND kind IN ('report','bonus_back')""", (user, spot_id)).fetchone()[0]
        if bonus > 0:
            award(c, user, -bonus, "bonus_back", spot_id)
        if s["seeded"]:
            c.execute("INSERT INTO events VALUES (?,?,?,?,?)", (user, "seed_removed", 0, spot_id, time.time()))
        names = json.loads(s["photos"]) + [n for n in (s["task_before_photo"], s["verify_photo"]) if n]
        for n in names:
            c.execute("DELETE FROM photos WHERE name=?", (n,))
        c.execute("DELETE FROM spots WHERE id=?", (spot_id,))
    return {"ok": True}


@app.post("/api/spots/{spot_id}/claim")
def claim_spot(spot_id: str, user: str = Form(...)):
    user = clean_name(user)
    with closing(db()) as c, c:
        s = get_spot(c, spot_id)
        if s["status"] in ("pending", "cleaned"):
            raise HTTPException(409, "This spot has already been cleaned.")
        if s["status"] == "claimed" and s["claimed_by"] != user:
            # claims expire after 24h so spots don't get stuck
            if time.time() - (s["claimed_at"] or 0) < 24 * 3600:
                raise HTTPException(409, f"{s['claimed_by']} is already on it.")
        own = s["reporter"] == user
        full = ai.points_for(s["difficulty"], s["bags"])
        if own and not (s["status"] == "claimed" and s["claimed_by"] == user):
            # Cleaning your own report = a solo cleanup: same limits as "I'll clean it myself"
            # (half points, daily cap, 60 s minimum, always checked), and the +5 report bonus goes back.
            check_solo_limit(c, user)
            got = c.execute("""SELECT COALESCE(SUM(points),0) FROM events WHERE user=? AND spot_id=?
                               AND kind IN ('report','bonus_back')""", (user, spot_id)).fetchone()[0]
            if got > 0:
                award(c, user, -got, "bonus_back", spot_id)
        seen(c, user)
        c.execute("""UPDATE spots SET status='claimed', claimed_by=?, claimed_at=?, task_before_photo=NULL,
                     self_clean=?, points=? WHERE id=?""",
                  (user, time.time(), 1 if own else 0,
                   round(full * SELF_CLEAN_FACTOR) if own else full, spot_id))
        row = get_spot(c, spot_id)
    return spot_out(row)


@app.post("/api/spots/{spot_id}/clean")
async def clean_spot(
    spot_id: str,
    user: str = Form(...),
    lat: float = Form(...),
    lng: float = Form(...),
    photo: UploadFile = File(...),
):
    user = clean_name(user)
    with closing(db()) as c:
        s = get_spot(c, spot_id)
    if s["status"] in ("pending", "cleaned"):
        raise HTTPException(409, "This spot has already been cleaned.")
    if s["status"] != "claimed" or s["claimed_by"] != user:
        raise HTTPException(409, "Claim this spot first.")
    require_on_site(s, lat, lng, "after photo")
    if s["self_clean"] and time.time() - (s["claimed_at"] or 0) < SELF_CLEAN_MIN_SECONDS:
        raise HTTPException(409, "That was fast! Finish cleaning, then take the after photo.")

    if not s["task_before_photo"]:
        raise HTTPException(409, "Take a before photo to start this cleanup first.")
    after = await read_photo(photo)
    before = [load_photo(p) for p in json.loads(s["photos"])] + [load_photo(s["task_before_photo"])]
    v = ai.verify_cleanup(before, after)

    if v.get("looks_fake"):
        strike_fake(user, spot_id)
    if not v["same_place"]:
        raise HTTPException(422, "The after photo doesn't look like the same place. " + v["comment"])
    if v["percent_cleaned"] < MIN_PERCENT_CLEANED:
        raise HTTPException(422, f"Only about {v['percent_cleaned']}% looks clean. " + v["comment"])

    earned = round(s["points"] * v["percent_cleaned"] / 100)
    with closing(db()) as c, c:
        seen(c, user, lat, lng)
        name = save_photo(*after, c=c)
        t = trust_of(c, user)
        # Solo cleanups and reporter cleanups always receive an independent check.
        skip = not s["self_clean"] and user != s["reporter"] and t["trusted"] and random.random() < TRUSTED_SKIP_RATE
        if skip:
            verifier, status_, awarded = None, "cleaned", None
        else:
            verifier = pick_verifier(c, s, {s["reporter"], user})
            status_, awarded = "pending", None
        cur = c.execute("""UPDATE spots SET status=?, cleaner=?, after_photo=?, verify_comment=?,
                           percent_cleaned=?, pending_points=?, cleaned_at=?, verifier=?,
                           verifier_assigned_at=?, excluded=?
                           WHERE id=? AND status='claimed' AND claimed_by=?""",
                        (status_, user, name, v["comment"], v["percent_cleaned"], earned, time.time(),
                         verifier, time.time() if verifier else None, json.dumps([]), spot_id, user))
        if cur.rowcount != 1:
            raise HTTPException(409, "Spot changed while verifying. Try again.")
        if skip:
            awarded = award(c, user, earned, "clean", spot_id)
            c.execute("UPDATE spots SET awarded=?, verified_at=?, verify_note=? WHERE id=?",
                      (awarded, time.time(), "Trusted volunteer: spot-check skipped.", spot_id))
        row = get_spot(c, spot_id)
    return {"spot": spot_out(row), "earned": earned, "awarded": awarded,
            "pending": not skip, "verifier": verifier, "verification": v}


@app.post("/api/spots/{spot_id}/start")
async def start_spot(spot_id: str, user: str = Form(...), lat: float = Form(...), lng: float = Form(...), photo: UploadFile = File(...)):
    user = clean_name(user)
    before = await read_photo(photo)
    with closing(db()) as c, c:
        s = get_spot(c, spot_id)
        if s["status"] != "claimed" or s["claimed_by"] != user:
            raise HTTPException(409, "Claim this spot first.")
        require_on_site(s, lat, lng, "before photo")
        name = save_photo(*before, c=c)
        c.execute("UPDATE spots SET task_before_photo=? WHERE id=? AND status='claimed' AND claimed_by=?",
                  (name, spot_id, user))
        row = get_spot(c, spot_id)
    return spot_out(row)


@app.post("/api/spots/{spot_id}/verify")
async def verify_spot(
    spot_id: str,
    user: str = Form(...),
    verdict: str = Form(...),          # "clean" | "dirty"
    lat: float = Form(...),
    lng: float = Form(...),
    photo: UploadFile = File(...),
):
    """A third person checks the cleanup in person. Only then are points paid."""
    user = clean_name(user)
    if verdict not in ("clean", "dirty"):
        raise HTTPException(400, "Verdict must be clean or dirty.")
    with closing(db()) as c:
        s = get_spot(c, spot_id)
    if s["status"] != "pending":
        raise HTTPException(409, "This spot isn't waiting for verification.")
    excluded = set(json.loads(s["excluded"] or "[]"))
    if user in (s["reporter"], s["cleaner"]):
        raise HTTPException(403, "Someone else has to verify this one. That's the point!")
    if user in excluded:
        raise HTTPException(403, "You already checked this spot.")
    out = spot_out(s)
    if s["verifier"] != user and not out["open_to_anyone"]:
        raise HTTPException(403, f"{s['verifier']} was picked to verify this spot.")
    require_on_site(s, lat, lng, "verification photo")

    check = await read_photo(photo)
    before = [load_photo(p) for p in json.loads(s["photos"])]
    v = ai.verify_cleanup(before, check)
    if v.get("looks_fake"):
        strike_fake(user, spot_id)
    if not v["same_place"]:
        raise HTTPException(422, "Your photo doesn't look like the same place. Take it where the trash was.")

    with closing(db()) as c, c:
        seen(c, user, lat, lng)
        name = save_photo(*check, c=c)
        if verdict == "clean":
            got = award(c, s["cleaner"], s["pending_points"] or 0, "clean", spot_id)
            reward = award(c, user, VERIFY_REWARD, "verify", spot_id)
            c.execute("""UPDATE spots SET status='cleaned', awarded=?, verified_by=?, verified_at=?,
                         verify_photo=?, verify_note=? WHERE id=? AND status='pending'""",
                      (got, user, time.time(), name, f"Confirmed clean by {user}.", spot_id))
            result = "confirmed"
        elif v["percent_cleaned"] >= 85:
            # Human says dirty but the AI sees it clean: don't punish yet, ask someone else.
            excluded.add(user)
            nxt = pick_verifier(c, s, excluded | {s["reporter"], s["cleaner"]})
            c.execute("""UPDATE spots SET verifier=?, verifier_assigned_at=?, excluded=? WHERE id=?""",
                      (nxt, time.time() if nxt else None, json.dumps(sorted(excluded)), spot_id))
            reward, result = 0, "disputed"
        else:
            award(c, s["cleaner"], 0, "rejected", spot_id)
            reward = award(c, user, VERIFY_REWARD, "verify", spot_id)
            c.execute("""UPDATE spots SET status='open', claimed_by=NULL, claimed_at=NULL, cleaner=NULL,
                         after_photo=NULL, pending_points=NULL, verifier=NULL, verifier_assigned_at=NULL,
                         excluded=NULL, self_clean=0, verify_photo=?, verify_note=? WHERE id=?""",
                      (name, f"{user} found it still dirty. Back on the map!", spot_id))
            result = "rejected"
        row = get_spot(c, spot_id)
    return {"spot": spot_out(row), "result": result, "reward": reward}


@app.post("/api/spots/{spot_id}/flag")
async def flag_spot(
    spot_id: str,
    user: str = Form(...),
    lat: float = Form(...),
    lng: float = Form(...),
    photo: UploadFile = File(...),
):
    """Anyone passing by can report a 'cleaned' spot as still dirty. Points are taken back."""
    user = clean_name(user)
    with closing(db()) as c:
        s = get_spot(c, spot_id)
    if s["status"] != "cleaned":
        raise HTTPException(409, "Only cleaned spots can be flagged.")
    if user == s["cleaner"]:
        raise HTTPException(403, "You cleaned this one.")
    require_on_site(s, lat, lng, "photo")
    img = await read_photo(photo)
    a = ai.assess_spot([img])
    if a.get("looks_fake"):
        strike_fake(user, spot_id)
    if not a["is_litter"]:
        raise HTTPException(422, "The AI doesn't see litter in your photo. Looks clean!")

    with closing(db()) as c, c:
        seen(c, user, lat, lng)
        name = save_photo(*img, c=c)
        if s["awarded"]:
            award(c, s["cleaner"], -s["awarded"], "revoke", spot_id)
        else:
            award(c, s["cleaner"], 0, "revoke", spot_id)
        reward = award(c, user, FLAG_REWARD, "flag", spot_id)
        photos = json.loads(s["photos"]) + [name]
        c.execute("""UPDATE spots SET status='open', photos=?, claimed_by=NULL, claimed_at=NULL, cleaner=NULL,
                     after_photo=NULL, pending_points=NULL, awarded=NULL, verifier=NULL,
                     verifier_assigned_at=NULL, excluded=NULL, verified_by=NULL, verified_at=NULL,
                     verify_photo=NULL, self_clean=0, verify_note=? WHERE id=?""",
                  (json.dumps(photos), f"{user} flagged it as still dirty. Points were taken back.", spot_id))
        row = get_spot(c, spot_id)
    return {"spot": spot_out(row), "reward": reward}


@app.post("/api/users/{name}/complain")
def complain(name: str, user: str = Form(...), reason: str = Form(...), spot_id: str | None = Form(None)):
    """Report a user (fake photos, littering...). 2+ complaints from different people lock donations."""
    name, user = clean_name(name), clean_name(user)
    if name == user:
        raise HTTPException(400, "You can't report yourself.")
    if reason not in COMPLAINT_REASONS:
        raise HTTPException(400, "Pick a reason.")
    with closing(db()) as c, c:
        active = c.execute("SELECT COUNT(*) FROM events WHERE user=?", (user,)).fetchone()[0]
        if not active:
            raise HTTPException(403, "Report, clean or check a spot first. Brand-new accounts can't file complaints.")
        cur = c.execute("INSERT INTO complaints VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                        (user, name, reason, spot_id, time.time()))
        if cur.rowcount == 0:
            raise HTTPException(409, f"You already reported {name}.")
        settle_donations(c, name)
    return {"ok": True}


@app.post("/api/users/{name}/donate")
def donate(name: str, user: str = Form(...), amount: float = Form(...)):
    """Support a proven volunteer. Recorded only (no real payment yet), held 7 days, refunded on any problem."""
    name, user = clean_name(name), clean_name(user)
    if name == user:
        raise HTTPException(400, "You can't donate to yourself.")
    cents = round(amount * 100)
    if not 100 <= cents <= 2000:
        raise HTTPException(400, "Donations are $1 to $20.")
    with closing(db()) as c, c:
        st = donation_status(c, name)
        if not st["eligible"]:
            raise HTTPException(403, f"{name} can't receive donations.")
        if cents > st["room_cents"]:
            raise HTTPException(409, f"{name} reached this month's donation limit "
                                     f"(${DON_MONTHLY_CAP_CENTS // 100}). Thanks anyway!")
        c.execute("INSERT INTO donations VALUES (?,?,?,?,?,?,?)",
                  (uuid.uuid4().hex[:12], user, name, cents, time.time(), "held", time.time() + DON_HOLD_S))
    return {"ok": True, "amount_cents": cents, "held_days": DON_HOLD_S // 86400}


@app.get("/api/leaderboard")
def leaderboard():
    with closing(db()) as c:
        rows = c.execute("""
            SELECT user, SUM(points) AS points,
                   SUM(CASE WHEN kind='clean' THEN 1 ELSE 0 END) AS cleanups, SUM(CASE WHEN kind='verify' THEN 1 ELSE 0 END) AS verifications
            FROM events GROUP BY user ORDER BY points DESC, cleanups DESC LIMIT 50""").fetchall()
    return [{**dict(r), "level": level_for(r["points"])["title"]} for r in rows]


@app.get("/api/users/{name}")
def profile(name: str):
    name = clean_name(name)
    with closing(db()) as c:
        r = c.execute("""SELECT COALESCE(SUM(points),0) AS points,
                                COALESCE(SUM(CASE WHEN kind='clean' THEN 1 ELSE 0 END),0) AS cleanups,
                                COALESCE(SUM(CASE WHEN kind='report' THEN 1 ELSE 0 END),0) AS reports,
                                COALESCE(SUM(CASE WHEN kind='verify' THEN 1 ELSE 0 END),0) AS verifications
                         FROM events WHERE user=?""", (name,)).fetchone()
        bags = c.execute("SELECT COALESCE(SUM(bags),0) FROM spots WHERE cleaner=? AND status='cleaned'",
                         (name,)).fetchone()[0]
        on_hold = c.execute("SELECT COALESCE(SUM(pending_points),0) FROM spots WHERE cleaner=? AND status='pending'",
                            (name,)).fetchone()[0]
        to_verify = [x[0] for x in c.execute(
            "SELECT id FROM spots WHERE status='pending' AND verifier=?", (name,)).fetchall()]
        trust = trust_of(c, name)
        ranked = c.execute("SELECT user FROM events GROUP BY user ORDER BY SUM(points) DESC").fetchall()
    with closing(db()) as c, c:
        settle_donations(c, name)
        don = donation_status(c, name)
        sums = dict(c.execute("""SELECT status, SUM(amount_cents) FROM donations WHERE cleaner=?
                                 GROUP BY status""", (name,)).fetchall())
    users = [x[0] for x in ranked]
    rank = users.index(name) + 1 if name in users else None
    return {"user": name, **dict(r), "bags": bags, "rank": rank, "total_users": len(users),
            "level": level_for(r["points"]), "on_hold": on_hold, "to_verify": to_verify,
            "trusted": trust["trusted"], "strikes": trust["strikes"],
            "donations": {**don, "held_cents": sums.get("held", 0), "released_cents": sums.get("released", 0),
                          "refunded_cents": sums.get("refunded", 0), "monthly_cap_cents": DON_MONTHLY_CAP_CENTS}}
