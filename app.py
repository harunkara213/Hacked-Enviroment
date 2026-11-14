"""Hacker Environment - report litter, clean it up, earn points.

Flow: report -> claim -> clean (AI pre-check, points ON HOLD) -> a random nearby
person checks it in person -> points paid. Anyone can later flag "still dirty".

Run:  .venv/bin/uvicorn app:app --reload --host 0.0.0.0 --port 8000
"""
import io
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

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import ai  # noqa: E402
import db as dbmod  # noqa: E402
import photocheck  # noqa: E402

BASE = Path(__file__).parent

MAX_PHOTOS = 4
MAX_PHOTO_BYTES = 8 * 1024 * 1024
DAILY_POINT_CAP = int(os.getenv("DAILY_POINT_CAP", "1000"))
MAX_CLEAN_DISTANCE_M = float(os.getenv("MAX_CLEAN_DISTANCE_M", "250"))
MAX_ACTION_LOCATION_ACCURACY_M = float(os.getenv("MAX_ACTION_LOCATION_ACCURACY_M", "75"))
MIN_PERCENT_CLEANED = 60
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}

FLAG_REWARD = 10

# Donations: only for proven, clean-record volunteers. Any strike or 2+ complaints = locked for good.
# DONATION_DEMO=1 lowers the bar so the flow can be shown at a hackathon.
_DEMO = os.getenv("DONATION_DEMO") == "1"
DON_MIN_CLEANUPS = 2 if _DEMO else 10      # approved cleanups of OTHER people's reports (solo ones don't count)
DON_MIN_REPORTERS = 2 if _DEMO else 5      # ...on spots reported by this many different people
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
    "detection_status": "TEXT",
    "detection_json": "TEXT",
    "location_accuracy_m": "REAL",
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
        CREATE TABLE IF NOT EXISTS client_rejects (
            ts REAL NOT NULL, user TEXT, reason TEXT, detail TEXT, agent TEXT
        );
        """)
        have = c.columns("spots")
        for col, typ in SPOT_EXTRA_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE spots ADD COLUMN {col} {typ}")
        if "dhash" not in c.columns("photos"):
            c.execute("ALTER TABLE photos ADD COLUMN dhash TEXT")
        # Fingerprint photos uploaded before this check existed.
        for r in c.execute("SELECT name, data FROM photos WHERE dhash IS NULL").fetchall():
            c.execute("UPDATE photos SET dhash=? WHERE name=?", (photocheck.dhash(bytes(r["data"])) or "", r["name"]))


init_db()


# ---------- helpers ----------

def clean_name(name: str) -> str:
    """Trim the name and match it to an existing user ignoring case, so "p", "P" and " P " are one
    person. Otherwise someone could report as "P" and verify their own cleanup as "p"."""
    name = " ".join((name or "").split())[:24]
    if not name:
        raise HTTPException(400, "Pick a username first.")
    with closing(db()) as c:
        r = c.execute("""SELECT name FROM users WHERE LOWER(name)=LOWER(?)
                         UNION ALL SELECT user FROM events WHERE LOWER(user)=LOWER(?)
                         UNION ALL SELECT reporter FROM spots WHERE LOWER(reporter)=LOWER(?)
                         LIMIT 1""", (name, name, name)).fetchone()
    return r[0] if r else name


def distance_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def validate_location_accuracy(accuracy_m):
    if accuracy_m is None:
        return
    if not math.isfinite(accuracy_m) or accuracy_m <= 0:
        raise HTTPException(400, "Invalid GPS accuracy reading.")
    if accuracy_m > MAX_ACTION_LOCATION_ACCURACY_M:
        raise HTTPException(422, f"GPS accuracy is about ±{int(accuracy_m)} m. Wait for a more accurate fix (±{int(MAX_ACTION_LOCATION_ACCURACY_M)} m or better), then retry.")


def require_on_site(spot, lat, lng, what="photo", accuracy_m=None):
    validate_location_accuracy(accuracy_m)
    dist = distance_m(lat, lng, spot["lat"], spot["lng"])
    if dist + (accuracy_m or 0) > MAX_CLEAN_DISTANCE_M:
        raise HTTPException(403, f"GPS puts you about {int(dist)} m away (±{int(accuracy_m or 0)} m). Get closer to the spot and take the {what} again.")


async def read_photo(f: UploadFile) -> tuple[bytes, str]:
    mime = (f.content_type or "").lower()
    if mime not in ALLOWED_MIME:
        raise HTTPException(400, f"Unsupported image type: {mime or 'unknown'}")
    data = await f.read()
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(400, "Photo is empty or too large (max 8 MB).")
    reject_if_reused(data)
    return data, mime


def reject_if_reused(data: bytes):
    """Every photo must be new: catches the same shot used as before and after, one photo for
    several spots, a verifier re-uploading the cleaner's photo, or someone else's upload."""
    h = photocheck.dhash(data)
    if not h:
        return
    with closing(db()) as c:
        rows = c.execute("SELECT dhash FROM photos WHERE dhash IS NOT NULL AND dhash != ''").fetchall()
    if any(photocheck.same_photo(h, r[0]) for r in rows):
        raise HTTPException(409, "This photo was already uploaded. Take a new photo at the spot.")


def save_photo(data: bytes, mime: str, c=None) -> str:
    """Photos live in the database (not on disk) so they survive Render restarts.
    Pass `c` when already inside a write transaction, so SQLite doesn't lock itself."""
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime]
    name = uuid.uuid4().hex + ext
    row = (name, mime, data, photocheck.dhash(data) or "")
    if c is not None:
        c.execute("INSERT INTO photos (name, mime, data, dhash) VALUES (?,?,?,?)", row)
    else:
        with closing(db()) as c2, c2:
            c2.execute("INSERT INTO photos (name, mime, data, dhash) VALUES (?,?,?,?)", row)
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
    try:
        d["detection"] = json.loads(d.get("detection_json") or "null")
    except (TypeError, json.JSONDecodeError):
        d["detection"] = None
    for k in ("after_photo", "verify_photo"):
        if d[k]:
            d[k] = "/uploads/" + d[k]
    if d.get("task_before_photo"):
        d["task_before_photo"] = "/uploads/" + d["task_before_photo"]
    return d


def get_spot(c, spot_id):
    s = c.execute("SELECT * FROM spots WHERE id=?", (spot_id,)).fetchone()
    if not s:
        raise HTTPException(404, "Spot not found.")
    return s


def seen(c, user, lat=None, lng=None):
    """Remember where users were last seen."""
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
    """Strikes = cleanups taken back after a "still dirty" flag (and older failed checks / fake photos)."""
    r = c.execute("""SELECT COALESCE(SUM(CASE WHEN kind='clean' THEN 1 ELSE 0 END),0) AS cleanups,
                            COALESCE(SUM(CASE WHEN kind IN ('rejected','revoke','fake') THEN 1 ELSE 0 END),0) AS strikes
                     FROM events WHERE user=?""", (user,)).fetchone()
    return {"cleanups": r["cleanups"], "strikes": r["strikes"], "trusted": r["cleanups"] >= 3 and r["strikes"] == 0}


def donation_status(c, user) -> dict:
    """Every rule must pass. Problems (strikes, complaints) lock donations permanently."""
    t = trust_of(c, user)
    v = c.execute("""SELECT COUNT(*) AS n, COUNT(DISTINCT reporter) AS reporters
                     FROM spots WHERE cleaner=? AND status='cleaned' AND reporter != cleaner""",
                  (user,)).fetchone()
    complaints = c.execute("SELECT COUNT(DISTINCT from_user) FROM complaints WHERE about_user=?",
                           (user,)).fetchone()[0]
    first = c.execute("SELECT MIN(ts) FROM events WHERE user=?", (user,)).fetchone()[0]
    days = (time.time() - first) / 86400 if first else 0
    month = c.execute("""SELECT COALESCE(SUM(amount_cents),0) FROM donations
                         WHERE cleaner=? AND status!='refunded' AND ts>?""",
                      (user, time.time() - 30 * 86400)).fetchone()[0]
    rules = [
        {"label": f"{DON_MIN_CLEANUPS} approved cleanups of others' reports", "have": v["n"],
         "need": DON_MIN_CLEANUPS, "ok": v["n"] >= DON_MIN_CLEANUPS},
        {"label": f"Spots reported by {DON_MIN_REPORTERS}+ different people", "have": v["reporters"],
         "need": DON_MIN_REPORTERS, "ok": v["reporters"] >= DON_MIN_REPORTERS},
        {"label": f"Member for {DON_MIN_DAYS}+ days", "have": int(days), "need": DON_MIN_DAYS,
         "ok": days >= DON_MIN_DAYS},
        {"label": "No \"still dirty\" flags, ever", "have": t["strikes"], "need": 0,
         "ok": t["strikes"] == 0, "permanent": True},
        # Internal rule: enforced, but never shown to users (complaints are handled internally).
        {"label": "No complaints", "have": complaints, "need": 0,
         "ok": complaints <= DON_MAX_COMPLAINTS, "permanent": True, "internal": True},
    ]
    locked = t["strikes"] > 0 or complaints > DON_MAX_COMPLAINTS
    return {"eligible": all(r["ok"] for r in rules), "locked": locked, "rules": rules,
            "complaints": complaints, "month_cents": month,
            "room_cents": max(0, DON_MONTHLY_CAP_CENTS - month)}


def public_donation_status(don, sums) -> dict:
    """What users may see: no complaint counts, no internal rules."""
    out = {k: v for k, v in don.items() if k != "complaints"}
    out["rules"] = [r for r in don["rules"] if not r.get("internal")]
    out.update(held_cents=sums.get("held", 0), released_cents=sums.get("released", 0),
               refunded_cents=sums.get("refunded", 0), monthly_cap_cents=DON_MONTHLY_CAP_CENTS)
    return out


def settle_donations(c, user):
    """Held donations: refund everything if the cleaner got locked, else release after the hold."""
    st = donation_status(c, user)
    if st["locked"]:
        c.execute("UPDATE donations SET status='refunded' WHERE cleaner=? AND status='held'", (user,))
    else:
        c.execute("UPDATE donations SET status='released' WHERE cleaner=? AND status='held' AND release_at<?",
                  (user, time.time()))


def settle_pending(c):
    """Cleanups that were waiting for the old in-person check: pay them now (the AI already approved them)."""
    for s in c.execute("SELECT * FROM spots WHERE status='pending'").fetchall():
        got = award(c, s["cleaner"], s["pending_points"] or 0, "clean", s["id"])
        c.execute("""UPDATE spots SET status='cleaned', awarded=?, verifier=NULL, verified_at=?,
                     verify_note=? WHERE id=?""", (got, time.time(), "Approved by the AI check.", s["id"]))


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

# More demo spots around Edmonton's parks and river valley. Photos are crops / mirror images of
# the demo photos ("crop" = left, top, right, bottom as fractions) so each spot looks different.
_R, _F, _L, _P = "roadside-dump.jpg", "field-dump.jpg", "park-lawn-picnic.jpg", "playground-benches.jpg"
DEMO_SPOTS += [
    dict(id="demo-kinsmen-trail", lat=53.5226, lng=-113.5098, file=_R, crop=(0, .35, .6, 1), reporter="Omar",
         difficulty=4, bags=8, minutes=150, hours_ago=3, hazards=["Broken glass possible"],
         summary="Plastic bags and food packaging piled along the trail edge by Kinsmen Park."),
    dict(id="demo-queen-elizabeth", lat=53.5287, lng=-113.4975, file=_L, crop=(.2, .3, 1, 1), reporter="Lina",
         difficulty=2, bags=2, minutes=35, hours_ago=6, hazards=[],
         summary="Leftover picnic wrappers and bottles scattered on the grass."),
    dict(id="demo-louise-mckinney", lat=53.5398, lng=-113.4841, file=_R, crop=(.4, .4, 1, 1), flip=True,
         reporter="Ava", difficulty=3, bags=5, minutes=90, hours_ago=9, hazards=["Wet waste"],
         summary="Food waste and bags dumped near the riverfront path."),
    dict(id="demo-victoria-park", lat=53.5352, lng=-113.5268, file=_F, crop=(0, .35, .7, 1), reporter="Leo",
         difficulty=4, bags=7, minutes=120, hours_ago=12, hazards=["Sharp debris"],
         summary="Paper, plastic and household rubbish spread across the open field."),
    dict(id="demo-mill-creek", lat=53.5190, lng=-113.4790, file=_R, crop=(.15, .3, .85, .95), reporter="Mia",
         difficulty=5, bags=12, minutes=200, hours_ago=26, hazards=["Sharp debris", "Heavy items"],
         summary="Large illegal dump at the ravine edge: bags, packaging and bulky items."),
    dict(id="demo-gallagher-park", lat=53.5321, lng=-113.4706, file=_L, crop=(0, .25, .75, 1), flip=True,
         reporter="Omar", difficulty=2, bags=3, minutes=40, hours_ago=4, hazards=[],
         summary="Party leftovers on the hill: cans, cups and snack bags."),
    dict(id="demo-laurier-park", lat=53.5182, lng=-113.5558, file=_P, crop=(0, .2, .8, 1), reporter="Lina",
         difficulty=1, bags=1, minutes=15, hours_ago=1, hazards=[],
         summary="Napkins and food scraps around the picnic benches."),
    dict(id="demo-buena-vista", lat=53.5236, lng=-113.5592, file=_F, crop=(.3, .4, 1, 1), flip=True,
         reporter="Ava", difficulty=3, bags=4, minutes=75, hours_ago=15, hazards=["Wet waste"],
         summary="Scattered plastic and paper in the off-leash area."),
    dict(id="demo-groat-ravine", lat=53.5372, lng=-113.5471, file=_R, crop=(0, .5, .5, 1), flip=True,
         reporter="Leo", difficulty=3, bags=4, minutes=70, hours_ago=7, hazards=["Steep slope"],
         summary="Bags of rubbish thrown down the ravine slope."),
    dict(id="demo-terwillegar", lat=53.4790, lng=-113.5882, file=_L, crop=(.1, .45, .9, 1), reporter="Mia",
         difficulty=2, bags=2, minutes=30, hours_ago=20, hazards=[],
         summary="Drink bottles and wrappers left near the trailhead parking."),
    dict(id="demo-whitemud-park", lat=53.4931, lng=-113.5481, file=_F, crop=(.1, .3, .9, .9), reporter="Omar",
         difficulty=4, bags=6, minutes=110, hours_ago=30, hazards=["Sharp debris"],
         summary="Dumped household waste in the grass by the creek."),
    dict(id="demo-fort-edmonton", lat=53.4992, lng=-113.5809, file=_R, crop=(.5, .35, 1, .95), reporter="Lina",
         difficulty=3, bags=5, minutes=85, hours_ago=40, hazards=[],
         summary="Plastic and food packaging along the footbridge path."),
    dict(id="demo-ezio-faraone", lat=53.5318, lng=-113.5112, file=_P, crop=(.2, .3, 1, 1), flip=True,
         reporter="Ava", difficulty=1, bags=1, minutes=10, hours_ago=2, hazards=[],
         summary="A few cups and napkins left on the lookout benches."),
    dict(id="demo-dawson-park", lat=53.5459, lng=-113.4621, file=_L, crop=(.3, .2, 1, .9), reporter="Leo",
         difficulty=2, bags=3, minutes=45, hours_ago=11, hazards=[],
         summary="Food wrappers, bottles and a plastic bag on the river flats."),
    dict(id="demo-rundle-park", lat=53.5452, lng=-113.3905, file=_R, crop=(0, .25, 1, 1), flip=True,
         reporter="Mia", difficulty=5, bags=10, minutes=180, hours_ago=48, hazards=["Heavy items", "Wet waste"],
         summary="Wide area of dumped rubbish along the back fence of the park."),
]


def demo_photo(d) -> bytes:
    """Demo photo bytes, cropped / mirrored if the spot says so."""
    data = (BASE / "static" / "demo" / d["file"]).read_bytes()
    if not d.get("crop") and not d.get("flip"):
        return data
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(data)).convert("RGB")
    if d.get("crop"):
        w, h = im.size
        l, t, r, b = d["crop"]
        im = im.crop((int(l * w), int(t * h), int(r * w), int(b * h)))
    if d.get("flip"):
        im = ImageOps.mirror(im)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return out.getvalue()


def seed_demo():
    if os.getenv("SEED_DEMO") != "1":
        return
    with closing(db()) as c, c:
        for d in DEMO_SPOTS:
            if c.execute("SELECT 1 FROM spots WHERE id=?", (d["id"],)).fetchone():
                continue
            if c.execute("SELECT 1 FROM events WHERE spot_id=? AND kind='seed_removed'", (d["id"],)).fetchone():
                continue  # deleted on purpose: don't bring it back
            name = save_photo(demo_photo(d), "image/jpeg", c=c)
            c.execute("""INSERT INTO spots (id, lat, lng, reporter, photos, difficulty, bags, minutes,
                         hazards, summary, points, ai_source, created_at, seeded)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                      (d["id"], d["lat"], d["lng"], d["reporter"], json.dumps([name]), d["difficulty"],
                       d["bags"], d["minutes"], json.dumps(d["hazards"]), d["summary"],
                       ai.points_for(d["difficulty"], d["bags"]), "demo-seed",
                       time.time() - d["hours_ago"] * 3600))
    seed_demo_people()


# Demo volunteers with a past history, so Ranks shows who can receive donations and who can't.
# Their past cleanups are stored as hidden spots (seeded=2): they count for points, donation
# rules and profiles, but don't appear on the map. Donation status is computed by the real rules.
DEMO_REPORTERS = ["Ava", "Leo", "Mia", "Omar", "Lina"]
DEMO_PEOPLE = [
    # name, cleanups, account age (days), extras
    ("Sofia", 12, 30, {"donations": [("Ava", 2000, "released"), ("Leo", 1500, "released")]}),
    ("Aylin", 10, 21, {"donations": [("Omar", 1000, "held")]}),
    ("Marcus", 6, 18, {}),
    ("Jin", 7, 25, {"still_dirty_flag": True}),       # caught once -> donations locked for good
    ("Priya", 5, 16, {"complaints": ["Ava", "Omar"]}),  # 2 complaints -> locked
    ("Noah", 1, 2, {}),                               # too new
]


def seed_demo_people():
    with closing(db()) as c, c:
        if c.execute("SELECT 1 FROM spots WHERE id LIKE 'demohist-%' LIMIT 1").fetchone():
            return
        photo = save_photo((BASE / "static" / "demo" / "park-lawn-picnic.jpg").read_bytes(), "image/jpeg", c=c)
        rnd = random.Random(7)
        now = time.time()
        for name, n, days, extra in DEMO_PEOPLE:
            start = now - days * 86400
            c.execute("INSERT INTO events VALUES (?,?,?,?,?)", (name, "joined", 0, None, start))
            for i in range(n):
                sid = f"demohist-{name.lower()}-{i}"
                diff, bags = rnd.randint(1, 4), rnd.randint(1, 5)
                pts = ai.points_for(diff, bags)
                ts = start + (i + 1) * (now - start) / (n + 1)
                reporter = DEMO_REPORTERS[i % len(DEMO_REPORTERS)]
                c.execute("""INSERT INTO spots (id, lat, lng, reporter, photos, difficulty, bags, minutes, hazards,
                             summary, points, ai_source, created_at, status, claimed_by, cleaner, after_photo,
                             percent_cleaned, awarded, cleaned_at, verify_note, seeded)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'cleaned',?,?,?,?,?,?,?,2)""",
                          (sid, 53.52 + rnd.uniform(-0.01, 0.01), -113.53 + rnd.uniform(-0.015, 0.015), reporter,
                           json.dumps([photo]), diff, bags, 15 * diff, "[]", "Past cleanup (demo history).", pts,
                           "demo-seed", ts - 3600, name, name, photo, 100, pts, ts, "Approved by the AI check."))
                c.execute("INSERT INTO events VALUES (?,?,?,?,?)", (reporter, "report", 5, sid, ts - 3600))
                c.execute("INSERT INTO events VALUES (?,?,?,?,?)", (name, "clean", pts, sid, ts))
            if extra.get("still_dirty_flag"):
                sid = f"demohist-{name.lower()}-0"
                got = c.execute("SELECT awarded FROM spots WHERE id=?", (sid,)).fetchone()[0]
                c.execute("INSERT INTO events VALUES (?,?,?,?,?)", (name, "revoke", -got, sid, now - 86400))
            for who in extra.get("complaints", []):
                c.execute("INSERT INTO complaints VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                          (who, name, "fake_cleanup", None, now - 2 * 86400))
            for donor, cents, status in extra.get("donations", []):
                c.execute("INSERT INTO donations VALUES (?,?,?,?,?,?,?)",
                          (uuid.uuid4().hex[:12], donor, name, cents, now - 3 * 86400, status,
                           now - 3 * 86400 + DON_HOLD_S))


seed_demo()
with closing(db()) as _c, _c:
    settle_pending(_c)


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
            "storage": "postgres" if dbmod.IS_PG else "sqlite (data is lost on restart)",
            # One knob for GPS strictness: set MAX_ACTION_LOCATION_ACCURACY_M on Render; the app reads it here.
            "max_location_accuracy_m": MAX_ACTION_LOCATION_ACCURACY_M,
            "recent_photo_rejections": recent_rejections()}


def recent_rejections():
    """Last photos the app refused before upload (reason, file info, phone), to spot false alarms."""
    with closing(db()) as c:
        rows = c.execute("SELECT ts, reason, detail, agent FROM client_rejects ORDER BY ts DESC LIMIT 5").fetchall()
    return [{"min_ago": round((time.time() - r["ts"]) / 60), "reason": r["reason"], "file": r["detail"],
             "device": r["agent"]} for r in rows]


@app.post("/api/client-reject")
def client_reject(request: Request, user: str = Form(""), reason: str = Form(""), detail: str = Form("")):
    ua = request.headers.get("user-agent", "")
    device = next((d for d in ("iPhone", "iPad", "Android", "Windows", "Macintosh", "Linux") if d in ua), "other")
    with closing(db()) as c, c:
        c.execute("INSERT INTO client_rejects VALUES (?,?,?,?,?)",
                  (time.time(), user[:24], reason[:200], detail[:120], f"{device}: {ua[:150]}"))
        c.execute("DELETE FROM client_rejects WHERE ts < ?", (time.time() - 7 * 86400,))
    return {"ok": True}


@app.post("/api/ping")
def ping(user: str = Form(...), lat: float | None = Form(None), lng: float | None = Form(None), accuracy_m: float | None = Form(None)):
    """App open: remember the user (and where they are) so they can be picked as a verifier."""
    user = clean_name(user)
    with closing(db()) as c, c:
        if lat is None or lng is None or (accuracy_m is not None and accuracy_m > MAX_ACTION_LOCATION_ACCURACY_M):
            c.execute("INSERT INTO users (name, lat, lng, seen) VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET lat=NULL, lng=NULL, seen=excluded.seen",
                      (user, None, None, time.time()))
        else:
            seen(c, user, lat, lng)
    return {"ok": True, "user": user}  # the app adopts this spelling, see clean_name


@app.get("/api/spots")
def list_spots(lat: float | None = None, lng: float | None = None):
    with closing(db()) as c, c:
        # seeded=2 = demo people's past cleanups: they count for points/profiles but stay off the map
        rows = c.execute("SELECT * FROM spots WHERE COALESCE(seeded, 0) != 2 ORDER BY created_at DESC").fetchall()
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
    accuracy_m: float | None = Form(None),
    photos: list[UploadFile] = File(...),
    self_clean: bool = Form(False),
):
    """Report a dirty spot. With self_clean=true the reporter cleans it themselves:
    these photos become the BEFORE photos and the spot is claimed by them right away."""
    user = clean_name(user)
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise HTTPException(400, "Invalid location.")
    validate_location_accuracy(accuracy_m)
    if not 1 <= len(photos) <= MAX_PHOTOS:
        raise HTTPException(400, f"Add 1 to {MAX_PHOTOS} photos.")
    if self_clean:
        with closing(db()) as c:
            check_solo_limit(c, user)
    images = [await read_photo(p) for p in photos]

    result = ai.assess_spot(images)
    if result["outcome"] == "no_litter_detected":
        raise HTTPException(422, {
            "message": "No litter detected in these photos.",
            "analysis": result,
        })

    spot_id = uuid.uuid4().hex[:10]
    names = [save_photo(d, m) for d, m in images]
    # Uncertain reports remain visible for human judgment, but cannot mint rewards.
    points = round(result["points"] * SELF_CLEAN_FACTOR) if self_clean else result["points"]
    now = time.time()
    with closing(db()) as c, c:
        seen(c, user, lat, lng)
        c.execute("""INSERT INTO spots (id, lat, lng, reporter, photos, difficulty, bags, minutes,
                     hazards, summary, points, ai_source, created_at, detection_status, detection_json, location_accuracy_m)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (spot_id, lat, lng, user, json.dumps(names), result["difficulty"],
                   result["estimated_bags"], result["estimated_minutes"], json.dumps(result["hazards"]),
                   result["summary"], points, result["source"], now, result["outcome"],
                   json.dumps({k: result[k] for k in ("outcome", "objects", "litter_confidence", "rationale")}), accuracy_m))
        if self_clean:
            c.execute("""UPDATE spots SET self_clean=1, status='claimed', claimed_by=?, claimed_at=?,
                         task_before_photo=? WHERE id=?""", (user, now, names[0], spot_id))
            bonus = 0
        else:
            bonus = award(c, user, 5, "report", spot_id) if result["outcome"] == "litter_detected" else 0
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
                     self_clean=0, points=? WHERE id=?""",
                  (0 if s["detection_status"] == "unable_to_determine" else ai.points_for(s["difficulty"], s["bags"]), spot_id))
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
        full = 0 if s["detection_status"] == "unable_to_determine" else ai.points_for(s["difficulty"], s["bags"])
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
    accuracy_m: float | None = Form(None),
    photo: UploadFile = File(...),
):
    user = clean_name(user)
    with closing(db()) as c:
        s = get_spot(c, spot_id)
    if s["status"] in ("pending", "cleaned"):
        raise HTTPException(409, "This spot has already been cleaned.")
    if s["status"] != "claimed" or s["claimed_by"] != user:
        raise HTTPException(409, "Claim this spot first.")
    require_on_site(s, lat, lng, "after photo", accuracy_m)
    wait = SELF_CLEAN_MIN_SECONDS - (time.time() - (s["claimed_at"] or 0))
    if s["self_clean"] and wait > 0:
        raise HTTPException(409, f"Cleaning your own spot needs at least {SELF_CLEAN_MIN_SECONDS} seconds. "
                                 f"Upload the after photo in {int(wait) + 1} s.")

    if not s["task_before_photo"]:
        raise HTTPException(409, "Take a before photo to start this cleanup first.")
    after = await read_photo(photo)
    before = [load_photo(p) for p in json.loads(s["photos"])] + [load_photo(s["task_before_photo"])]
    v = ai.verify_cleanup(before, after)

    if not v["same_place"]:
        raise HTTPException(422, "The after photo doesn't look like the same place. " + v["comment"])
    if v["percent_cleaned"] < MIN_PERCENT_CLEANED:
        raise HTTPException(422, f"Only about {v['percent_cleaned']}% looks clean. " + v["comment"])

    # The AI approved the before/after pair: points are paid right away. Anyone passing by can
    # still flag the spot as "still dirty", which takes the points back.
    earned = round(s["points"] * v["percent_cleaned"] / 100)
    with closing(db()) as c, c:
        seen(c, user, lat, lng)
        name = save_photo(*after, c=c)
        cur = c.execute("""UPDATE spots SET status='cleaned', cleaner=?, after_photo=?, verify_comment=?,
                           percent_cleaned=?, pending_points=NULL, cleaned_at=?, verified_at=?, verify_note=?
                           WHERE id=? AND status='claimed' AND claimed_by=?""",
                        (user, name, v["comment"], v["percent_cleaned"], time.time(), time.time(),
                         "Approved by the AI check.", spot_id, user))
        if cur.rowcount != 1:
            raise HTTPException(409, "Spot changed while checking. Try again.")
        awarded = award(c, user, earned, "clean", spot_id)
        c.execute("UPDATE spots SET awarded=? WHERE id=?", (awarded, spot_id))
        row = get_spot(c, spot_id)
    return {"spot": spot_out(row), "earned": earned, "awarded": awarded,
            "capped": awarded < earned, "verification": v}


@app.post("/api/spots/{spot_id}/start")
async def start_spot(spot_id: str, user: str = Form(...), lat: float = Form(...), lng: float = Form(...), accuracy_m: float | None = Form(None), photo: UploadFile = File(...)):
    user = clean_name(user)
    before = await read_photo(photo)
    with closing(db()) as c, c:
        s = get_spot(c, spot_id)
        if s["status"] != "claimed" or s["claimed_by"] != user:
            raise HTTPException(409, "Claim this spot first.")
        require_on_site(s, lat, lng, "before photo", accuracy_m)
        name = save_photo(*before, c=c)
        c.execute("UPDATE spots SET task_before_photo=? WHERE id=? AND status='claimed' AND claimed_by=?",
                  (name, spot_id, user))
        row = get_spot(c, spot_id)
    return spot_out(row)


@app.post("/api/spots/{spot_id}/flag")
async def flag_spot(
    spot_id: str,
    user: str = Form(...),
    lat: float = Form(...),
    lng: float = Form(...),
    accuracy_m: float | None = Form(None),
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
    require_on_site(s, lat, lng, "photo", accuracy_m)
    img = await read_photo(photo)
    a = ai.assess_spot([img])
    if a["outcome"] != "litter_detected":
        message = ("Unable to determine whether litter remains." if a["outcome"] == "unable_to_determine"
                   else "No litter detected in your photo.")
        raise HTTPException(422, {"message": message, "analysis": a})

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
        out = []
        for r in rows:
            d = donation_status(c, r["user"])
            out.append({**dict(r), "level": level_for(r["points"])["title"],
                        "donatable": d["eligible"]})
    return out


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
            "donations": public_donation_status(don, sums)}
