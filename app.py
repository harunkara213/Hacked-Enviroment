"""Hacker Environment - report litter, clean it up, earn points.

Run:  .venv/bin/uvicorn app:app --reload --host 0.0.0.0 --port 8000
"""
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import ai  # noqa: E402

BASE = Path(__file__).parent
DB_PATH = Path(os.getenv("DB_PATH", BASE / "data.db"))
UPLOADS = Path(os.getenv("UPLOADS_DIR", BASE / "uploads"))
UPLOADS.mkdir(parents=True, exist_ok=True)

MAX_PHOTOS = 4
MAX_PHOTO_BYTES = 8 * 1024 * 1024
DAILY_POINT_CAP = int(os.getenv("DAILY_POINT_CAP", "1000"))
MAX_CLEAN_DISTANCE_M = float(os.getenv("MAX_CLEAN_DISTANCE_M", "250"))
MIN_PERCENT_CLEANED = 60
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}

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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
        """)


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


async def read_photo(f: UploadFile) -> tuple[bytes, str]:
    mime = (f.content_type or "").lower()
    if mime not in ALLOWED_MIME:
        raise HTTPException(400, f"Unsupported image type: {mime or 'unknown'}")
    data = await f.read()
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(400, "Photo is empty or too large (max 8 MB).")
    return data, mime


def save_photo(data: bytes, mime: str) -> str:
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime]
    name = uuid.uuid4().hex + ext
    (UPLOADS / name).write_bytes(data)
    return name


def load_photo(name: str) -> tuple[bytes, str]:
    ext = Path(name).suffix
    mime = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[ext]
    return (UPLOADS / name).read_bytes(), mime


def spot_out(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["photos"] = ["/uploads/" + p for p in json.loads(d["photos"])]
    d["hazards"] = json.loads(d["hazards"] or "[]")
    if d["after_photo"]:
        d["after_photo"] = "/uploads/" + d["after_photo"]
    return d


def points_today(c, user: str) -> int:
    since = time.time() - 24 * 3600
    row = c.execute("SELECT COALESCE(SUM(points),0) FROM events WHERE user=? AND ts>?",
                    (user, since)).fetchone()
    return row[0]


def level_for(points: int) -> dict:
    title, floor, nxt = LEVELS[0][1], 0, None
    for i, (p, t) in enumerate(LEVELS):
        if points >= p:
            title, floor = t, p
            nxt = LEVELS[i + 1][0] if i + 1 < len(LEVELS) else None
    return {"title": title, "floor": floor, "next": nxt}


# ---------- app ----------

app = FastAPI(title="Hacker Environment")
app.mount("/uploads", StaticFiles(directory=UPLOADS), name="uploads")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/manifest.json")
def manifest():
    return FileResponse(BASE / "static" / "manifest.json")


@app.get("/api/status")
def status():
    return {"ai": "gemini" if ai._get_client() else "demo", "model": ai.MODEL}


@app.get("/api/spots")
def list_spots():
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM spots ORDER BY created_at DESC").fetchall()
    return [spot_out(r) for r in rows]


@app.post("/api/spots")
async def report_spot(
    user: str = Form(...),
    lat: float = Form(...),
    lng: float = Form(...),
    photos: list[UploadFile] = File(...),
):
    user = clean_name(user)
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise HTTPException(400, "Invalid location.")
    if not 1 <= len(photos) <= MAX_PHOTOS:
        raise HTTPException(400, f"Add 1 to {MAX_PHOTOS} photos.")
    images = [await read_photo(p) for p in photos]

    result = ai.assess_spot(images)
    if not result["is_litter"]:
        raise HTTPException(422, "The AI couldn't find litter in these photos. Try a clearer shot of the trash.")

    spot_id = uuid.uuid4().hex[:10]
    names = [save_photo(d, m) for d, m in images]
    with closing(db()) as c, c:
        c.execute("""INSERT INTO spots (id, lat, lng, reporter, photos, difficulty, bags, minutes,
                     hazards, summary, points, ai_source, created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (spot_id, lat, lng, user, json.dumps(names), result["difficulty"],
                   result["estimated_bags"], result["estimated_minutes"], json.dumps(result["hazards"]),
                   result["summary"], result["points"], result["source"], time.time()))
        # small reward for reporting, also capped
        bonus = min(5, max(0, DAILY_POINT_CAP - points_today(c, user)))
        c.execute("INSERT INTO events VALUES (?,?,?,?,?)", (user, "report", bonus, spot_id, time.time()))
        row = c.execute("SELECT * FROM spots WHERE id=?", (spot_id,)).fetchone()
    return {"spot": spot_out(row), "report_bonus": bonus}


@app.post("/api/spots/{spot_id}/claim")
def claim_spot(spot_id: str, user: str = Form(...)):
    user = clean_name(user)
    with closing(db()) as c, c:
        s = c.execute("SELECT * FROM spots WHERE id=?", (spot_id,)).fetchone()
        if not s:
            raise HTTPException(404, "Spot not found.")
        if s["status"] == "cleaned":
            raise HTTPException(409, "This spot is already clean.")
        if s["reporter"] == user:
            raise HTTPException(403, "You can't clean a spot you reported. Let someone else take it!")
        if s["status"] == "claimed" and s["claimed_by"] != user:
            # claims expire after 24h so spots don't get stuck
            if time.time() - (s["claimed_at"] or 0) < 24 * 3600:
                raise HTTPException(409, f"{s['claimed_by']} is already on it.")
        c.execute("UPDATE spots SET status='claimed', claimed_by=?, claimed_at=? WHERE id=?",
                  (user, time.time(), spot_id))
        row = c.execute("SELECT * FROM spots WHERE id=?", (spot_id,)).fetchone()
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
        s = c.execute("SELECT * FROM spots WHERE id=?", (spot_id,)).fetchone()
    if not s:
        raise HTTPException(404, "Spot not found.")
    if s["status"] == "cleaned":
        raise HTTPException(409, "This spot is already clean.")
    if s["reporter"] == user:
        raise HTTPException(403, "You can't clean a spot you reported.")
    if s["status"] != "claimed" or s["claimed_by"] != user:
        raise HTTPException(409, "Claim this spot first.")
    dist = distance_m(lat, lng, s["lat"], s["lng"])
    if dist > MAX_CLEAN_DISTANCE_M:
        raise HTTPException(403, f"You're {int(dist)} m away. Take the after photo at the spot.")

    after = await read_photo(photo)
    before = [load_photo(p) for p in json.loads(s["photos"])]
    v = ai.verify_cleanup(before, after)

    if not v["same_place"]:
        raise HTTPException(422, "The after photo doesn't look like the same place. " + v["comment"])
    if v["percent_cleaned"] < MIN_PERCENT_CLEANED:
        raise HTTPException(422, f"Only about {v['percent_cleaned']}% looks clean. " + v["comment"])

    with closing(db()) as c, c:
        earned = round(s["points"] * v["percent_cleaned"] / 100)
        awarded = max(0, min(earned, DAILY_POINT_CAP - points_today(c, user)))
        name = save_photo(*after)
        cur = c.execute("""UPDATE spots SET status='cleaned', cleaner=?, after_photo=?, verify_comment=?,
                           percent_cleaned=?, awarded=?, cleaned_at=?
                           WHERE id=? AND status='claimed' AND claimed_by=?""",
                        (user, name, v["comment"], v["percent_cleaned"], awarded, time.time(), spot_id, user))
        if cur.rowcount != 1:
            raise HTTPException(409, "Spot changed while verifying. Try again.")
        c.execute("INSERT INTO events VALUES (?,?,?,?,?)", (user, "clean", awarded, spot_id, time.time()))
        row = c.execute("SELECT * FROM spots WHERE id=?", (spot_id,)).fetchone()
    return {"spot": spot_out(row), "awarded": awarded, "earned": earned,
            "capped": awarded < earned, "verification": v}


@app.get("/api/leaderboard")
def leaderboard():
    with closing(db()) as c:
        rows = c.execute("""
            SELECT user, SUM(points) AS points,
                   SUM(kind='clean') AS cleanups, SUM(kind='report') AS reports
            FROM events GROUP BY user ORDER BY points DESC, cleanups DESC LIMIT 50""").fetchall()
    return [{**dict(r), "level": level_for(r["points"])["title"]} for r in rows]


@app.get("/api/users/{name}")
def profile(name: str):
    name = clean_name(name)
    with closing(db()) as c:
        r = c.execute("""SELECT COALESCE(SUM(points),0) AS points,
                                COALESCE(SUM(kind='clean'),0) AS cleanups,
                                COALESCE(SUM(kind='report'),0) AS reports
                         FROM events WHERE user=?""", (name,)).fetchone()
        bags = c.execute("SELECT COALESCE(SUM(bags),0) FROM spots WHERE cleaner=?", (name,)).fetchone()[0]
        ranked = c.execute("SELECT user FROM events GROUP BY user ORDER BY SUM(points) DESC").fetchall()
    users = [x[0] for x in ranked]
    rank = users.index(name) + 1 if name in users else None
    return {"user": name, **dict(r), "bags": bags, "rank": rank, "total_users": len(users),
            "level": level_for(r["points"])}
