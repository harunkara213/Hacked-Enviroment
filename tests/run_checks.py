"""End-to-end checks for the game rules and anti-cheat. Run before every push:

    .venv/bin/python tests/run_checks.py                  # SQLite (temporary file)
    DATABASE_URL=postgresql://... .venv/bin/python tests/run_checks.py   # an EMPTY Postgres

The AI is replaced by a stub (no API key or cost needed). Every photo is a fresh random
image, because the app rejects photos that were already uploaded.
Exit code is 1 if any check fails.
"""
import io
import os
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("DB_PATH", f"{tempfile.mkdtemp()}/checks.db")
os.environ.update(DONATION_DEMO="1", SEED_DEMO="1", GEMINI_API_KEY="")
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import ai  # noqa: E402
import app as A  # noqa: E402

c = TestClient(A.app)
HERE = {"lat": "53.5232", "lng": "-113.5263"}
FAILED = []
random.seed(2026)  # reproducible test photos


def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILED.append(msg)
    return cond


# ---------- AI stubs ----------
ai_says = {"outcome": "litter_detected", "same_place": True, "percent": 100}


def stub_assess(images):
    lit = ai_says["outcome"] == "litter_detected"
    return {"outcome": ai_says["outcome"], "objects": [], "litter_confidence": 90, "rationale": "stub",
            "is_litter": lit, "difficulty": 3, "estimated_bags": 4, "estimated_minutes": 50, "hazards": [],
            "summary": "Stub litter.", "points": ai.points_for(3, 4) if lit else 0, "source": "stub"}


def stub_verify(before, after):
    return {"same_place": ai_says["same_place"], "percent_cleaned": ai_says["percent"], "comment": "stub", "source": "stub"}


ai.assess_spot, ai.verify_cleanup = stub_assess, stub_verify
FULL = ai.points_for(3, 4)


def new_photo():
    """A unique random JPEG made of coloured shapes (behaves like a real photo when resized)."""
    im = Image.new("RGB", (320, 240), tuple(random.randrange(256) for _ in range(3)))
    d = ImageDraw.Draw(im)
    for _ in range(25):
        x, y = random.randrange(320), random.randrange(240)
        d.ellipse((x, y, x + random.randrange(20, 120), y + random.randrange(20, 90)),
                  fill=tuple(random.randrange(256) for _ in range(3)))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=85)
    return b.getvalue()


def img(data=None, key="photo"):
    return {key: ("p.jpg", data or new_photo(), "image/jpeg")}


def post(url, files=None, **data):
    return c.post(url, data={**HERE, **data}, files=files)


def report(user, **extra):
    return post("/api/spots", files=[("photos", ("p.jpg", new_photo(), "image/jpeg"))], user=user, **extra)


def points(user):
    return c.get(f"/api/users/{user}").json()["points"]


def full_cleanup(reporter, cleaner):
    sid = report(reporter).json()["spot"]["id"]
    c.post(f"/api/spots/{sid}/claim", data={"user": cleaner})
    post(f"/api/spots/{sid}/start", files=img(), user=cleaner)
    return post(f"/api/spots/{sid}/clean", files=img(), user=cleaner)


print("== before photo step")
sid = report("rep1").json()["spot"]["id"]
c.post(f"/api/spots/{sid}/claim", data={"user": "rep2"})
ok(post(f"/api/spots/{sid}/clean", files=img(), user="rep2").status_code == 409, "can't clean before taking a before photo")
ok(c.post(f"/api/spots/{sid}/start", data={"user": "rep2", "lat": "53.6", "lng": "-113.5263"}, files=img()).status_code == 403,
   "before photo from far away refused")
ok(post(f"/api/spots/{sid}/start", files=img(), user="v1").status_code == 409, "non-claimer can't start")
r = post(f"/api/spots/{sid}/start", files=img(), user="rep2")
ok(r.status_code == 200 and r.json()["task_before_photo"], "before photo saved")

print("== AI approval pays right away")
before = points("rep2")
r = post(f"/api/spots/{sid}/clean", files=img(), user="rep2")
ok(r.status_code == 200 and r.json()["spot"]["status"] == "cleaned", "cleanup approved -> spot is cleaned")
ok(r.json()["awarded"] == FULL and points("rep2") == before + FULL, "points paid immediately")
ok(c.post(f"/api/spots/{sid}/verify", data={"user": "v1", "verdict": "clean"}).status_code in (404, 405), "verify endpoint is gone")
ai_says.update(same_place=False)
r = full_cleanup("rep1", "bob")
ok(r.status_code == 422, "AI says different place -> refused, no points")
ai_says.update(same_place=True, percent=40)
r = full_cleanup("rep1", "bob")
ok(r.status_code == 422, "AI says only 40% clean -> refused")
ai_says.update(percent=80)
r = full_cleanup("rep1", "bob")
ok(r.status_code == 200 and r.json()["awarded"] == round(FULL * 0.8), "80% clean -> 80% of the points")
ai_says.update(percent=100)
ok(c.get("/api/spots").json() and all(s["status"] != "pending" for s in c.get("/api/spots").json()), "no spot ever waits for a check")

print("== still dirty flag")
s = full_cleanup("rep1", "v2").json()["spot"]
pts = points("v2")
ok(post(f"/api/spots/{s['id']}/flag", files=img(), user="v2").status_code == 403, "cleaner can't flag own spot")
ai_says.update(outcome="no_litter_detected")
ok(post(f"/api/spots/{s['id']}/flag", files=img(), user="rep2").status_code == 422, "flag refused if the AI sees no litter")
ai_says.update(outcome="litter_detected")
r = post(f"/api/spots/{s['id']}/flag", files=img(), user="rep2")
ok(r.status_code == 200 and r.json()["spot"]["status"] == "open", "flag reopens the spot")
ok(points("v2") == pts and c.get("/api/users/v2").json()["strikes"] == 0, "1st flag: places get dirty again, no penalty")
s2 = full_cleanup("rep1", "v2").json()["spot"]
post(f"/api/spots/{s2['id']}/flag", files=img(), user="rep2")
ok(c.get("/api/users/v2").json()["strikes"] == 0, "2nd flag: still no penalty")
s3 = full_cleanup("rep1", "v2").json()["spot"]
pts = points("v2")
post(f"/api/spots/{s3['id']}/flag", files=img(), user="rep2")
ok(points("v2") == pts - FULL and c.get("/api/users/v2").json()["strikes"] == 1, "3rd flag: points taken back + strike")

print("== reused photos")
same = new_photo()
s = report("rep1").json()["spot"]["id"]
c.post(f"/api/spots/{s}/claim", data={"user": "bob"})
ok(post(f"/api/spots/{s}/start", files=img(same), user="bob").status_code == 200, "before photo accepted")
ok(post(f"/api/spots/{s}/clean", files=img(same), user="bob").status_code == 409, "same photo as before and after is refused")
reenc = io.BytesIO()
Image.open(io.BytesIO(same)).resize((240, 180)).save(reenc, "JPEG", quality=75)
ok(post(f"/api/spots/{s}/clean", files=img(reenc.getvalue()), user="bob").status_code == 409, "re-saved/shrunk copy is also refused")
ok(post("/api/spots", files=[("photos", ("p.jpg", same, "image/jpeg"))], user="v1").status_code == 409,
   "a photo already used can't start a new report")

print("== donations")
ok(c.post("/api/users/bob/donate", data={"user": "donor", "amount": 5}).status_code == 403, "donating to non-eligible is refused")
full_cleanup("rep1", "dan")
full_cleanup("rep2", "dan")
ok(c.get("/api/users/dan").json()["donations"]["eligible"], "eligible after approved cleanups of 2 different people's reports")
ok(c.post("/api/users/dan/donate", data={"user": "dan", "amount": 5}).status_code == 400, "self-donation refused")
ok(c.post("/api/users/dan/donate", data={"user": "donor", "amount": 50}).status_code == 400, "$50 single donation refused")
ok(c.post("/api/users/dan/donate", data={"user": "donor", "amount": 20}).status_code == 200, "$20 donation accepted")
c.post("/api/users/dan/donate", data={"user": "donor", "amount": 20})
ok(c.post("/api/users/dan/donate", data={"user": "donor", "amount": 20}).status_code == 409, "monthly $50 cap enforced")
d = c.get("/api/users/dan").json()["donations"]
ok(d["held_cents"] == 4000 and d["released_cents"] == 0, "donations are held, not released")
ok(not c.get("/api/users/v2").json()["donations"]["eligible"] and c.get("/api/users/v2").json()["donations"]["locked"],
   "a 3rd 'still dirty' flag locks donations")

print("== complaints")
ok(c.post("/api/users/dan/complain", data={"user": "nobody", "reason": "fake_photos"}).status_code == 403, "brand-new account can't complain")
ok(c.post("/api/users/dan/complain", data={"user": "rep2", "reason": "littering"}).status_code == 200, "first complaint filed")
ok(c.post("/api/users/dan/complain", data={"user": "rep2", "reason": "littering"}).status_code == 409, "duplicate complaint ignored")
ok(c.get("/api/users/dan").json()["donations"]["eligible"], "one complaint: still eligible")
c.post("/api/users/dan/complain", data={"user": "rep1", "reason": "fake_photos"})
d = c.get("/api/users/dan").json()["donations"]
ok(d["locked"] and not d["eligible"], "two complaints: locked")
ok(d["refunded_cents"] == 4000 and d["held_cents"] == 0, "held donations refunded on lock")

print("== solo cleanups")
r = report("sam", self_clean="true").json()["spot"]
ok(r["status"] == "claimed" and r["claimed_by"] == "sam" and r["self_clean"] == 1 and r["task_before_photo"], "solo report: claimed by finder")
ok(r["points"] == round(FULL * 0.5), "solo cleanup is half points")
ok(c.get("/api/users/sam").json()["reports"] == 0, "no report bonus for solo cleanups")
ok(c.post(f"/api/spots/{r['id']}/claim", data={"user": "rep2"}).status_code == 409, "others can't grab a solo cleanup in progress")
w = post(f"/api/spots/{r['id']}/clean", files=img(), user="sam")
ok(w.status_code == 409 and " s." in w.json()["detail"], "after photo right away is refused, says seconds left")
A.SELF_CLEAN_MIN_SECONDS = 0
x = post(f"/api/spots/{r['id']}/clean", files=img(), user="sam").json()
ok(x["awarded"] == round(FULL * 0.5) and points("sam") == round(FULL * 0.5), "solo cleanup paid at half points")
ok(c.get("/api/users/sam").json()["donations"]["rules"][0]["have"] == 0, "solo cleanups don't count toward donations")
report("sam", self_clean="true"), report("sam", self_clean="true")
ok(report("sam", self_clean="true").status_code == 429, "max 3 solo cleanups per day")

print("== cleaning your own report")
s2 = report("rep3").json()["spot"]
ok(points("rep3") == 5, "normal report gives +5")
r = c.post(f"/api/spots/{s2['id']}/claim", data={"user": "rep3"})
ok(r.status_code == 200 and r.json()["self_clean"] == 1 and r.json()["points"] == round(FULL * 0.5), "own report = solo, half points")
ok(points("rep3") == 0, "report bonus taken back")
c.post(f"/api/spots/{s2['id']}/claim", data={"user": "rep3"})
ok(points("rep3") == 0, "re-claiming doesn't take the bonus twice")
A.SELF_CLEAN_MIN_SECONDS = 60
post(f"/api/spots/{s2['id']}/start", files=img(), user="rep3")
ok(post(f"/api/spots/{s2['id']}/clean", files=img(), user="rep3").status_code == 409, "own report: 60 s minimum applies")
A.SELF_CLEAN_MIN_SECONDS = 0
s3 = report("rep3").json()["spot"]
c.post(f"/api/spots/{s3['id']}/claim", data={"user": "rep3"})
with A.db() as conn:
    conn.execute("UPDATE spots SET claimed_at=? WHERE id=?", (A.time.time() - 25 * 3600, s3["id"]))
r = c.post(f"/api/spots/{s3['id']}/claim", data={"user": "rep2"}).json()
ok(r["points"] == FULL and r["self_clean"] == 0, "another volunteer gets full points back")
for _ in range(3):
    c.post(f"/api/spots/{report('rep4').json()['spot']['id']}/claim", data={"user": "rep4"})
ok(c.post(f"/api/spots/{report('rep4').json()['spot']['id']}/claim", data={"user": "rep4"}).status_code == 429,
   "max 3 own-report cleanups a day")

print("== names ignore case")
ok(c.post("/api/ping", data={"user": "  SAM "}).json()["user"] == "sam", "names are matched ignoring case and spaces")
own = report("Dana").json()["spot"]
ok(c.post(f"/api/spots/{own['id']}/claim", data={"user": "dana"}).json()["self_clean"] == 1,
   "claiming your own report under another case still counts as solo")

print("== demo spots, delete, give up")
ids = {s["id"] for s in c.get("/api/spots").json()}
ok({"demo-emily-murphy", "demo-windsor-playground", "demo-hawrelak-field"} <= ids, "3 demo park spots seeded")
A.seed_demo()
ok(len([s for s in c.get("/api/spots").json() if s["id"].startswith("demo-")]) == len(A.DEMO_SPOTS), "restart doesn't duplicate demo spots")
ok(c.post("/api/spots/demo-windsor-playground/delete", data={"user": "Mallory"}).status_code == 403, "others can't delete a report")
ok(c.post("/api/spots/demo-windsor-playground/delete", data={"user": "Leo"}).status_code == 200, "reporter deletes their report")
A.seed_demo()
ok("demo-windsor-playground" not in {s["id"] for s in c.get("/api/spots").json()}, "deleted demo spot doesn't come back")
h = report("Hello").json()["spot"]
ok(c.post(f"/api/spots/{h['id']}/delete", data={"user": "Hello"}).status_code == 200, "delete own report")
ok(points("Hello") == 0, "+5 taken back on delete")
ok(c.get(h["photos"][0]).status_code == 404, "deleted report's photo is removed")
h2 = report("Hello").json()["spot"]
c.post(f"/api/spots/{h2['id']}/claim", data={"user": "Heloo"})
ok(c.post(f"/api/spots/{h2['id']}/delete", data={"user": "Hello"}).status_code == 409, "can't delete while someone is cleaning")
ok(c.post(f"/api/spots/{h2['id']}/release", data={"user": "Mallory"}).status_code == 409, "only the claimer can give up")
r = c.post(f"/api/spots/{h2['id']}/release", data={"user": "Heloo"}).json()
ok(r["status"] == "open" and r["claimed_by"] is None and r["points"] == h2["points"], "give up: back to open, full points")

print("== old pending cleanups are paid out")
p = report("rep1").json()["spot"]["id"]
with A.db() as conn:
    conn.execute("UPDATE spots SET status='pending', cleaner='old', claimed_by='old', pending_points=42 WHERE id=?", (p,))
with A.db() as conn:
    A.settle_pending(conn)
ok(points("old") == 42 and next(s for s in c.get("/api/spots").json() if s["id"] == p)["status"] == "cleaned",
   "a cleanup left waiting for the old check is approved and paid")

print("== GPS accuracy")
g = report("rep1").json()["spot"]["id"]
c.post(f"/api/spots/{g}/claim", data={"user": "bob"})
ok(post(f"/api/spots/{g}/start", files=img(), user="bob", accuracy_m="400").status_code == 422, "imprecise GPS (±400 m) refused")
ok(post(f"/api/spots/{g}/start", files=img(), user="bob", accuracy_m="15").status_code == 200, "precise GPS (±15 m) accepted")
ok(c.get("/api/status").json()["max_location_accuracy_m"] == A.MAX_ACTION_LOCATION_ACCURACY_M, "status tells the app the GPS limit")

print("== app-side photo rejections are logged")
ok(c.post("/api/client-reject", data={"user": "Tester", "reason": "This photo is not new.", "detail": "image/jpeg 900 KB, age 45 min"},
          headers={"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0)"}).status_code == 200, "rejection report accepted")
rj = c.get("/api/status").json()["recent_photo_rejections"]
ok(rj and rj[0]["reason"] == "This photo is not new." and rj[0]["device"].startswith("iPhone"), "status shows recent rejections with device")

print("== demo people on Ranks")
lb = {r["user"]: r for r in c.get("/api/leaderboard").json()}
ok(all(lb[n]["donatable"] for n in ("Sofia", "Aylin", "Marcus")), "Sofia, Aylin, Marcus can receive donations")
locked = lambda n: c.get(f"/api/users/{n}").json()["donations"]["locked"]
ok(locked("Jin") and locked("Priya"), "Jin (still-dirty flag) and Priya (complaints) are locked")
ok(not lb["Noah"]["donatable"] and not locked("Noah"), "Noah is not there yet")
ok("donation_locked" not in lb["Jin"] and not lb["Jin"]["donatable"], "Ranks only says who is verified, not who is locked")
pd = c.get("/api/users/Priya").json()["donations"]
ok("complaints" not in pd and not any("omplaint" in r["label"] for r in pd["rules"]), "complaints are never shown to users")
ok(not any(s["id"].startswith("demohist-") for s in c.get("/api/spots").json()), "demo people's past cleanups stay off the map")
A.seed_demo()
ok(c.get("/api/users/Sofia").json()["cleanups"] == 12, "restart doesn't duplicate demo people")

print("== donation goals")
sg = c.get("/api/users/Sofia").json()["goal"]
ok(sg and sg["promise_kg"] == 100 and sg["target_cents"] == 5000 and sg["raised_cents"] == 3500, "Sofia's demo promise: 100 kg for $50, $35 raised")
ok(c.post("/api/users/Noah/goal", data={"user": "Noah", "promise_kg": 50, "target_dollars": 20}).status_code == 403,
   "only verified volunteers can make a promise")
ok(c.post("/api/users/Marcus/goal", data={"user": "Sofia", "promise_kg": 50, "target_dollars": 20}).status_code == 403,
   "you can't set someone else's promise")
ok(c.post("/api/users/Marcus/goal", data={"user": "Marcus", "promise_kg": 50, "target_dollars": 80}).status_code == 400,
   "a promise can't ask for more than the $50 monthly limit")
r = c.post("/api/users/Marcus/goal", data={"user": "Marcus", "promise_kg": 80, "target_dollars": 40})
ok(r.status_code == 200 and r.json()["goal"]["raised_cents"] == 0, "Marcus promises 80 kg for $40")
c.post("/api/users/Marcus/donate", data={"user": "donor", "amount": 15})
ok(c.get("/api/users/Marcus").json()["goal"]["raised_cents"] == 1500, "donations count toward the promise")
lb2 = {x["user"]: x for x in c.get("/api/leaderboard").json()}
ok(lb2["Marcus"]["goal"]["promise_kg"] == 80 and lb2["Jin"]["goal"] is None, "Ranks shows promises only for verified people")

print(f"\n{'ALL PASSED' if not FAILED else f'{len(FAILED)} FAILED'}")
sys.exit(1 if FAILED else 0)
