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
os.environ.update(DONATION_DEMO="1", SEED_DEMO="1", GEMINI_API_KEY="", TRUSTED_SKIP_RATE="0")
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

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


# ---------- stubs ----------
fake = {"on": False}


def stub_assess(images):
    return {"outcome": "litter_detected", "objects": [], "litter_confidence": 90, "rationale": "stub",
            "is_litter": True, "difficulty": 3, "estimated_bags": 4, "estimated_minutes": 50, "hazards": [],
            "summary": "Stub litter.", "looks_fake": fake["on"], "points": ai.points_for(3, 4), "source": "stub"}


def stub_verify(before, after):
    return {"same_place": True, "percent_cleaned": 100, "comment": "stub", "looks_fake": fake["on"], "source": "stub"}


ai.assess_spot, ai.verify_cleanup = stub_assess, stub_verify


def new_photo():
    """A unique random JPEG made of coloured shapes (behaves like a real photo when resized)."""
    from PIL import ImageDraw
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


for u in ["rep1", "rep2", "bob", "v1", "v2", "donor"]:
    post("/api/ping", user=u)


def full_cleanup(reporter, cleaner):
    sid = report(reporter).json()["spot"]["id"]
    c.post(f"/api/spots/{sid}/claim", data={"user": cleaner})
    post(f"/api/spots/{sid}/start", files=img(), user=cleaner)
    r = post(f"/api/spots/{sid}/clean", files=img(), user=cleaner).json()
    post(f"/api/spots/{sid}/verify", files=img(), user=r["verifier"], verdict="clean")
    return r["verifier"]


print("== before photo step")
sid = report("rep1").json()["spot"]["id"]
c.post(f"/api/spots/{sid}/claim", data={"user": "rep2"})
ok(post(f"/api/spots/{sid}/clean", files=img(), user="rep2").status_code == 409, "can't clean before taking a before photo")
ok(c.post(f"/api/spots/{sid}/start", data={"user": "rep2", "lat": "53.6", "lng": "-113.5263"}, files=img()).status_code == 403,
   "before photo from far away refused")
ok(post(f"/api/spots/{sid}/start", files=img(), user="v1").status_code == 409, "non-claimer can't start")
r = post(f"/api/spots/{sid}/start", files=img(), user="rep2")
ok(r.status_code == 200 and r.json()["task_before_photo"], "before photo saved")
r = post(f"/api/spots/{sid}/clean", files=img(), user="rep2")
ok(r.status_code == 200 and r.json()["pending"], "clean goes to pending (in-person check)")

print("== reused photos")
same = new_photo()
s = report("rep1").json()["spot"]["id"]
c.post(f"/api/spots/{s}/claim", data={"user": "bob"})
ok(post(f"/api/spots/{s}/start", files=img(same), user="bob").status_code == 200, "before photo accepted")
ok(post(f"/api/spots/{s}/clean", files=img(same), user="bob").status_code == 409, "same photo as before and after is refused")
reenc = io.BytesIO()
Image.open(io.BytesIO(same)).resize((240, 180)).save(reenc, "JPEG", quality=75)
ok(post(f"/api/spots/{s}/clean", files=img(reenc.getvalue()), user="bob").status_code == 409, "re-saved/shrunk copy is also refused")
after = new_photo()
x = post(f"/api/spots/{s}/clean", files=img(after), user="bob").json()
ok(post(f"/api/spots/{s}/verify", files=img(after), user=x["verifier"], verdict="clean").status_code == 409,
   "verifier can't re-upload the cleaner's photo")
ok(post("/api/spots", files=[("photos", ("p.jpg", same, "image/jpeg"))], user="v1").status_code == 409,
   "a photo already used can't start a new report")
post(f"/api/spots/{s}/verify", files=img(), user=x["verifier"], verdict="clean")

print("== donations")
d = c.get("/api/users/bob").json()["donations"]
ok(c.post("/api/users/v1/donate", data={"user": "donor", "amount": 5}).status_code == 403, "donating to non-eligible is refused")
vs = {full_cleanup("rep1", "bob")}
for _ in range(6):
    if len(vs) >= 2:
        break
    vs.add(full_cleanup("rep2", "bob"))
ok(c.get("/api/users/bob").json()["donations"]["eligible"], f"bob eligible after verified cleanups by {sorted(vs)}")
ok(c.post("/api/users/bob/donate", data={"user": "bob", "amount": 5}).status_code == 400, "self-donation refused")
ok(c.post("/api/users/bob/donate", data={"user": "donor", "amount": 50}).status_code == 400, "$50 single donation refused")
ok(c.post("/api/users/bob/donate", data={"user": "donor", "amount": 20}).status_code == 200, "$20 donation accepted")
c.post("/api/users/bob/donate", data={"user": "donor", "amount": 20})
ok(c.post("/api/users/bob/donate", data={"user": "donor", "amount": 20}).status_code == 409, "monthly $50 cap enforced")
d = c.get("/api/users/bob").json()["donations"]
ok(d["held_cents"] == 4000 and d["released_cents"] == 0, "donations are held, not released")

print("== complaints")
ok(c.post("/api/users/bob/complain", data={"user": "nobody", "reason": "fake_photos"}).status_code == 403, "brand-new account can't complain")
ok(c.post("/api/users/bob/complain", data={"user": "rep2", "reason": "littering"}).status_code == 200, "first complaint filed")
ok(c.post("/api/users/bob/complain", data={"user": "rep2", "reason": "littering"}).status_code == 409, "duplicate complaint ignored")
ok(c.get("/api/users/bob").json()["donations"]["eligible"], "one complaint: still eligible")
c.post("/api/users/bob/complain", data={"user": "rep1", "reason": "fake_photos"})
d = c.get("/api/users/bob").json()["donations"]
ok(d["locked"] and not d["eligible"], "two complaints: locked")
ok(d["refunded_cents"] == 4000 and d["held_cents"] == 0, "held donations refunded on lock")

print("== fake photos")
s = report("rep1").json()["spot"]["id"]
c.post(f"/api/spots/{s}/claim", data={"user": "v2"})
post(f"/api/spots/{s}/start", files=img(), user="v2")
fake["on"] = True
r = post(f"/api/spots/{s}/clean", files=img(), user="v2")
ok(r.status_code == 422 and "fake" in str(r.json()["detail"]), "fake after photo rejected")
ok(report("rep2").status_code == 422, "fake report photo rejected")
fake["on"] = False
p = c.get("/api/users/v2").json()
ok(p["strikes"] == 1 and p["donations"]["locked"], "fake photo = strike + donations locked")

print("== solo cleanups")
r = report("sam", self_clean="true").json()["spot"]
ok(r["status"] == "claimed" and r["claimed_by"] == "sam" and r["self_clean"] == 1 and r["task_before_photo"], "solo report: claimed by finder")
ok(r["points"] == round(ai.points_for(3, 4) * 0.5), "solo cleanup is half points")
ok(c.get("/api/users/sam").json()["reports"] == 0, "no report bonus for solo cleanups")
ok(c.post(f"/api/spots/{r['id']}/claim", data={"user": "rep2"}).status_code == 409, "others can't grab a solo cleanup in progress")
ok(post(f"/api/spots/{r['id']}/clean", files=img(), user="sam").status_code == 409, "after photo right away is refused (min 60 s)")
A.SELF_CLEAN_MIN_SECONDS = 0
x = post(f"/api/spots/{r['id']}/clean", files=img(), user="sam").json()
ok(x["pending"] and x["verifier"] not in (None, "sam"), "solo cleanup always gets an in-person check")
ok(post(f"/api/spots/{r['id']}/verify", files=img(), user="sam", verdict="clean").status_code == 403, "can't verify your own cleanup")
post(f"/api/spots/{r['id']}/verify", files=img(), user=x["verifier"], verdict="clean")
p = c.get("/api/users/sam").json()
ok(p["points"] == x["earned"], "points paid after the check")
ok(p["donations"]["rules"][0]["have"] == 0, "solo cleanups don't count toward donations")
report("sam", self_clean="true"), report("sam", self_clean="true")
ok(report("sam", self_clean="true").status_code == 429, "max 3 solo cleanups per day")

print("== cleaning your own report")
s2 = report("rep3").json()["spot"]
ok(c.get("/api/users/rep3").json()["points"] == 5, "normal report gives +5")
r = c.post(f"/api/spots/{s2['id']}/claim", data={"user": "rep3"})
ok(r.status_code == 200 and r.json()["self_clean"] == 1 and r.json()["points"] == round(s2["points"] * 0.5), "own report = solo, half points")
ok(c.get("/api/users/rep3").json()["points"] == 0, "report bonus taken back")
c.post(f"/api/spots/{s2['id']}/claim", data={"user": "rep3"})
ok(c.get("/api/users/rep3").json()["points"] == 0, "re-claiming doesn't take the bonus twice")
A.SELF_CLEAN_MIN_SECONDS = 60
post(f"/api/spots/{s2['id']}/start", files=img(), user="rep3")
ok(post(f"/api/spots/{s2['id']}/clean", files=img(), user="rep3").status_code == 409, "own report: 60 s minimum applies")
A.SELF_CLEAN_MIN_SECONDS = 0
s3 = report("rep3").json()["spot"]
c.post(f"/api/spots/{s3['id']}/claim", data={"user": "rep3"})
with A.db() as conn:
    conn.execute("UPDATE spots SET claimed_at=? WHERE id=?", (A.time.time() - 25 * 3600, s3["id"]))
r = c.post(f"/api/spots/{s3['id']}/claim", data={"user": "rep2"}).json()
ok(r["points"] == s3["points"] and r["self_clean"] == 0, "another volunteer gets full points back")
for _ in range(3):
    c.post(f"/api/spots/{report('rep4').json()['spot']['id']}/claim", data={"user": "rep4"})
ok(c.post(f"/api/spots/{report('rep4').json()['spot']['id']}/claim", data={"user": "rep4"}).status_code == 429,
   "max 3 own-report cleanups a day")

print("== demo spots, delete, give up")
ids = {s["id"] for s in c.get("/api/spots").json()}
ok({"demo-emily-murphy", "demo-windsor-playground", "demo-hawrelak-field"} <= ids, "3 demo park spots seeded")
A.seed_demo()
ok(len([s for s in c.get("/api/spots").json() if s["id"].startswith("demo-")]) == 3, "restart doesn't duplicate demo spots")
ok(c.post("/api/spots/demo-windsor-playground/delete", data={"user": "Mallory"}).status_code == 403, "others can't delete a report")
ok(c.post("/api/spots/demo-windsor-playground/delete", data={"user": "Leo"}).status_code == 200, "reporter deletes their report")
A.seed_demo()
ok("demo-windsor-playground" not in {s["id"] for s in c.get("/api/spots").json()}, "deleted demo spot doesn't come back")
h = report("Hello").json()["spot"]
ok(c.post(f"/api/spots/{h['id']}/delete", data={"user": "Hello"}).status_code == 200, "delete own report")
ok(c.get("/api/users/Hello").json()["points"] == 0, "+5 taken back on delete")
ok(c.get(h["photos"][0]).status_code == 404, "deleted report's photo is removed")
h2 = report("Hello").json()["spot"]
c.post(f"/api/spots/{h2['id']}/claim", data={"user": "Heloo"})
ok(c.post(f"/api/spots/{h2['id']}/delete", data={"user": "Hello"}).status_code == 409, "can't delete while someone is cleaning")
ok(c.post(f"/api/spots/{h2['id']}/release", data={"user": "Mallory"}).status_code == 409, "only the claimer can give up")
r = c.post(f"/api/spots/{h2['id']}/release", data={"user": "Heloo"}).json()
ok(r["status"] == "open" and r["claimed_by"] is None and r["points"] == h2["points"], "give up: back to open, full points")

print("== GPS accuracy")
g = report("rep1").json()["spot"]["id"]
c.post(f"/api/spots/{g}/claim", data={"user": "bob"})
ok(post(f"/api/spots/{g}/start", files=img(), user="bob", accuracy_m="400").status_code == 422, "imprecise GPS (±400 m) refused")
ok(post(f"/api/spots/{g}/start", files=img(), user="bob", accuracy_m="15").status_code == 200, "precise GPS (±15 m) accepted")
ok(c.get("/api/status").json()["max_location_accuracy_m"] == A.MAX_ACTION_LOCATION_ACCURACY_M, "status tells the app the GPS limit")

print("== names ignore case")
cs = report("Casey", self_clean="true").json()["spot"]
A.SELF_CLEAN_MIN_SECONDS = 0
x = post(f"/api/spots/{cs['id']}/clean", files=img(), user="Casey").json()
ok(x["verifier"] is not None and x["verifier"].lower() != "casey", "verifier is never the cleaner under another case")
ok(post(f"/api/spots/{cs['id']}/verify", files=img(), user="casey", verdict="clean").status_code == 403, "'casey' can't verify 'Casey's cleanup")
ok(c.post("/api/ping", data={"user": "  CASEY "}).json()["user"] == "Casey", "names are matched ignoring case and spaces")
own = report("Dana").json()["spot"]
r = c.post(f"/api/spots/{own['id']}/claim", data={"user": "dana"}).json()
ok(r["self_clean"] == 1, "claiming your own report under another case still counts as solo")
A.SELF_CLEAN_MIN_SECONDS = 60
w = report("Eli", self_clean="true").json()["spot"]
r = post(f"/api/spots/{w['id']}/clean", files=img(), user="Eli")
ok(r.status_code == 409 and " s." in r.json()["detail"], "too-early after photo says how many seconds are left")

print(f"\n{'ALL PASSED' if not FAILED else f'{len(FAILED)} FAILED'}")
sys.exit(1 if FAILED else 0)
