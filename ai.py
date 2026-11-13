"""AI helpers: score a dirty spot and verify a cleanup with Gemini vision.

If GEMINI_API_KEY is missing or the call fails, a deterministic "demo" result
is returned so the live demo never breaks. Results say which path was used.
"""
import hashlib
import os

from pydantic import BaseModel, Field

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

_client = None


def _get_client():
    global _client
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=key)
    return _client


# ---------- schemas ----------

class Assessment(BaseModel):
    is_litter: bool = Field(description="True if the photos show litter/trash that a volunteer could clean up")
    difficulty: int = Field(description="Cleanup difficulty from 1 (a few items) to 5 (large dump, hazards, heavy items)")
    estimated_bags: int = Field(description="Estimated number of standard 50L garbage bags needed")
    estimated_minutes: int = Field(description="Estimated minutes for one person to clean it")
    hazards: list[str] = Field(description="Hazards to warn volunteers about, e.g. broken glass, needles. Empty if none")
    summary: str = Field(description="One short sentence describing the spot for volunteers")


class Verification(BaseModel):
    same_place: bool = Field(description="True if the AFTER photo plausibly shows the same location as the BEFORE photos")
    percent_cleaned: int = Field(description="0-100: how much of the litter from BEFORE is gone in AFTER")
    comment: str = Field(description="One short, encouraging sentence for the volunteer about the result")


ASSESS_PROMPT = """You are the scoring engine for a community cleanup app.
Volunteers earn points for cleaning the spot in these photos.
Look at all photos (they may show the same spot from different angles) and assess the litter.
If the photos do not show litter (e.g. a selfie, a clean room, a random object), set is_litter=false.
Be realistic: a few cans = difficulty 1, a picnic mess = 2-3, an illegal dump or hazardous items = 4-5."""

VERIFY_PROMPT = """You are verifying a volunteer cleanup for a community cleanup app.
The first image(s) are BEFORE photos of a dirty spot. The LAST image is the AFTER photo the volunteer took.
Decide whether the AFTER photo shows the same place, and how much of the litter is gone.
Be fair but strict: a photo of a different clean place is NOT a cleanup."""


# ---------- scoring ----------

def points_for(difficulty: int, bags: int) -> int:
    difficulty = max(1, min(5, difficulty))
    bags = max(0, min(20, bags))
    return min(400, difficulty * difficulty * 10 + bags * 5)


def _parts(images: list[tuple[bytes, str]]):
    from google.genai import types
    return [types.Part.from_bytes(data=data, mime_type=mime) for data, mime in images]


def _generate(contents, schema):
    from google.genai import types
    resp = _get_client().models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.2,
        ),
    )
    parsed = resp.parsed
    if parsed is None:
        parsed = schema.model_validate_json(resp.text)
    return parsed


def _seed(images) -> int:
    h = hashlib.sha256()
    for data, _ in images:
        h.update(data[:4096])
    return int(h.hexdigest(), 16)


def assess_spot(images: list[tuple[bytes, str]]) -> dict:
    """images: list of (bytes, mime). Returns dict with assessment + points + source."""
    if _get_client():
        try:
            a = _generate(_parts(images) + [ASSESS_PROMPT], Assessment)
            a.difficulty = max(1, min(5, a.difficulty))
            a.estimated_bags = max(0, a.estimated_bags)
            return {**a.model_dump(), "points": points_for(a.difficulty, a.estimated_bags), "source": "gemini"}
        except Exception as e:  # network, quota, billing... keep the demo alive
            print(f"[ai] assess failed, using demo mode: {e!r}")
    s = _seed(images)
    difficulty = 1 + s % 5
    bags = difficulty + s % 3
    return {
        "is_litter": True,
        "difficulty": difficulty,
        "estimated_bags": bags,
        "estimated_minutes": 10 * difficulty + 5 * bags,
        "hazards": [],
        "summary": "Litter reported here (demo mode: AI offline).",
        "points": points_for(difficulty, bags),
        "source": "demo",
    }


def verify_cleanup(before: list[tuple[bytes, str]], after: tuple[bytes, str]) -> dict:
    if _get_client():
        try:
            v = _generate(_parts(before) + _parts([after]) + [VERIFY_PROMPT], Verification)
            v.percent_cleaned = max(0, min(100, v.percent_cleaned))
            return {**v.model_dump(), "source": "gemini"}
        except Exception as e:
            print(f"[ai] verify failed, using demo mode: {e!r}")
    return {"same_place": True, "percent_cleaned": 100,
            "comment": "Nice work! (demo mode: AI offline)", "source": "demo"}
