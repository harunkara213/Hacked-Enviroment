"""AI helpers: score a dirty spot and verify a cleanup with Gemini vision.

If GEMINI_API_KEY is missing or the call fails, a deterministic "demo" result
is returned so the live demo never breaks. Results say which path was used.
"""
import hashlib
import os

from pydantic import BaseModel, Field

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

_client = None
LAST_ERROR = None  # exposed by /api/status so setup and API failures are visible


def _get_client():
    global _client
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        global LAST_ERROR
        LAST_ERROR = "GEMINI_API_KEY is not configured. Add it to the service environment to enable photo analysis."
        return None
    if _client is None:
        try:
            from google import genai
            from google.genai import types
            # JSON mode (response_schema) is only available on v1beta.
            _client = genai.Client(api_key=key, http_options=types.HttpOptions(api_version="v1beta"))
            LAST_ERROR = None
        except Exception as e:
            LAST_ERROR = f"Gemini setup failed: {e!r}"[:300]
            print(f"[ai] Gemini client setup failed: {e!r}")
            return None
    return _client


# ---------- schemas ----------

class Assessment(BaseModel):
    is_litter: bool = Field(description="True if any photo visibly shows discarded human-made waste outdoors or in a public place")
    litter_confidence: int = Field(description="0-100 confidence in the litter decision; use a low score when the photo is too distant, dark, blurry, or obstructed to tell")
    difficulty: int = Field(description="Cleanup difficulty from 1 (a few items) to 5 (large dump, hazards, heavy items)")
    estimated_bags: int = Field(description="Estimated number of standard 50L garbage bags needed")
    estimated_minutes: int = Field(description="Estimated minutes for one person to clean it")
    hazards: list[str] = Field(description="Hazards to warn volunteers about, e.g. broken glass, needles. Empty if none")
    summary: str = Field(description="One short sentence describing the spot for volunteers")
    looks_fake: bool = Field(description="True if any photo looks like a photo of a screen, a screenshot, a stock/web image, or AI-generated/edited")


class Verification(BaseModel):
    same_place: bool = Field(description="True if the AFTER photo plausibly shows the same location as the BEFORE photos")
    percent_cleaned: int = Field(description="0-100: how much of the litter from BEFORE is gone in AFTER")
    comment: str = Field(description="One short, encouraging sentence for the volunteer about the result")
    looks_fake: bool = Field(description="True if the AFTER photo looks like a photo of a screen, a screenshot, a stock/web image, or AI-generated/edited")


FAKE_RULE = """Also check for cheating: set looks_fake=true only if a photo is clearly a picture of a screen
(moire, pixels, bezels), a screenshot, a stock/web image (watermark, studio look) or AI-generated/edited.
Normal phone photos, even blurry or dark ones, are NOT fake."""

ASSESS_PROMPT = """You assess reports for a community litter cleanup app. Examine every image carefully, including the ground, edges, gutters, bushes, and background. The images may show the same spot from different angles; evidence in any one image counts.

Count discarded human-made waste that a volunteer could pick up: bottles, cans, cups, wrappers, food packaging, paper, cardboard, bags, cigarette butts, broken household items, and dumped rubbish. A single recognizable discarded item is enough. Litter can be small, partly hidden, or far from the camera; do not require a large pile. In a public or outdoor place, treat an ambiguous man-made item as litter when it appears abandoned rather than in use.

Set is_litter=false only when you are confident that none of the photos shows discarded human-made waste. Do not mistake leaves, rocks, shadows, stains, or ordinary dirt for litter. If the image is too dark, blurry, distant, or obstructed to decide, set litter_confidence low and explain what detail is unclear in the summary; do not claim the place is clean.

For a confirmed report, give a short summary naming the visible waste. Be realistic about effort: one or two small items = difficulty 1, a scattered handful = 2, a larger area or several bags = 3, a substantial dump = 4-5. Estimate bags, minutes, and hazards from what is visible, not from the photo quality.
""" + FAKE_RULE

VERIFY_PROMPT = """You are verifying a volunteer cleanup for a community cleanup app.
The first image(s) are BEFORE photos of a dirty spot. The LAST image is the AFTER photo the volunteer took.
Decide whether the AFTER photo shows the same place, and how much of the litter is gone.
Be fair but strict: a photo of a different clean place is NOT a cleanup.
""" + FAKE_RULE


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
    client = _get_client()
    if client:
        try:
            a = _generate(_parts(images) + [ASSESS_PROMPT], Assessment)
            global LAST_ERROR
            LAST_ERROR = None
            a.difficulty = max(1, min(5, a.difficulty))
            a.estimated_bags = max(0, a.estimated_bags)
            a.litter_confidence = max(0, min(100, a.litter_confidence))
            return {**a.model_dump(), "points": points_for(a.difficulty, a.estimated_bags), "source": "gemini"}
        except Exception as e:  # network, quota, billing... keep the demo alive
            LAST_ERROR = repr(e)[:300]
            print(f"[ai] assess failed, using demo mode: {e!r}")
    s = _seed(images)
    difficulty = 1 + s % 5
    bags = difficulty + s % 3
    return {
        "is_litter": True,
        "litter_confidence": 0,
        "difficulty": difficulty,
        "estimated_bags": bags,
        "estimated_minutes": 10 * difficulty + 5 * bags,
        "hazards": [],
        "summary": "Litter reported here (demo mode: AI offline).",
        "looks_fake": False,
        "points": points_for(difficulty, bags),
        "source": "demo",
    }


def verify_cleanup(before: list[tuple[bytes, str]], after: tuple[bytes, str]) -> dict:
    client = _get_client()
    if client:
        try:
            v = _generate(_parts(before) + _parts([after]) + [VERIFY_PROMPT], Verification)
            global LAST_ERROR
            LAST_ERROR = None
            v.percent_cleaned = max(0, min(100, v.percent_cleaned))
            return {**v.model_dump(), "source": "gemini"}
        except Exception as e:
            LAST_ERROR = repr(e)[:300]
            print(f"[ai] verify failed, using demo mode: {e!r}")
    return {"same_place": True, "percent_cleaned": 100, "looks_fake": False,
            "comment": "Nice work! (demo mode: AI offline)", "source": "demo"}
