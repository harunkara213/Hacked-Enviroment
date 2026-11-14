"""AI helpers: score a dirty spot and verify a cleanup with Gemini vision.

If GEMINI_API_KEY is missing or the call fails, a deterministic "demo" result
is returned so the live demo never breaks. Results say which path was used.
"""
import io
import os
from typing import Literal

from pydantic import BaseModel, Field

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")  # cheap + fast; thinking is off (see _config)
# If MODEL is missing or not enabled for this key, fall back to a model we know works.
FALLBACK_MODEL = "gemini-2.5-flash"
ACTIVE_MODEL = MODEL  # what we actually use; shown on /api/status
NO_LITTER_MIN_CONFIDENCE = 70
LITTER_MIN_CONFIDENCE = 60

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
    outcome: Literal["litter_detected", "no_litter_detected", "unable_to_determine"] = Field(description="Use unable_to_determine if a waste-capable object is visible but whether it was discarded is unclear, or image evidence is insufficient")
    objects: list["DetectedObject"] = Field(description="Recognized waste-capable objects, including objects that appear intact/in use; empty only when no such object is visible")
    litter_confidence: int = Field(description="0-100 confidence in the discard/litter decision")
    rationale: str = Field(description="Short explanation of object recognition and the visible context used to judge whether it was discarded")
    difficulty: int = Field(description="Cleanup difficulty from 1 (a few items) to 5 (large dump, hazards, heavy items)")
    estimated_bags: int = Field(description="Estimated number of standard 50L garbage bags needed")
    estimated_minutes: int = Field(description="Estimated minutes for one person to clean it")
    hazards: list[str] = Field(description="Hazards to warn volunteers about, e.g. broken glass, needles. Empty if none")
    summary: str = Field(description="One short sentence describing the spot for volunteers")


class DetectedObject(BaseModel):
    object_type: str = Field(description="Specific recognized object, such as glass food jar, plastic bottle, aluminum can, wrapper, tissue, paper/cardboard, or plastic bag")
    material: str = Field(description="Best visible material, or unknown")
    count: int = Field(description="Number of distinct physical objects; deduplicate across photos and crops")
    object_confidence: int = Field(description="0-100 confidence that this object and type are correctly recognized")
    discard_status: Literal["likely_discarded", "likely_not_discarded", "uncertain"] = Field(description="Whether the object appears abandoned/discarded based on scene context, not just its category")
    disposal_category: str = Field(description="Suggested disposal stream if identifiable, otherwise unknown")
    context: str = Field(description="Brief visible evidence for its discard status")


Assessment.model_rebuild()


class Verification(BaseModel):
    same_place: bool = Field(description="True if the AFTER photo plausibly shows the same location as the BEFORE photos")
    percent_cleaned: int = Field(description="0-100: how much of the litter from BEFORE is gone in AFTER")
    comment: str = Field(description="One short, encouraging sentence for the volunteer about the result")



ASSESS_PROMPT = """You assess photos for a community litter cleanup app. First recognize waste-capable objects; then separately decide whether each appears discarded from visible scene context. Recognize glass jars and bottles, plastic bottles and containers, aluminum cans, food packaging, plastic bags, paper, cardboard, wrappers, tissues, and other discarded human-made materials. Report intact/useful objects too; do not equate object category with litter.

Examine all image areas. A single small item can be litter. Use floor/ground placement, isolation, dirt, damage, surrounding refuse, public/outdoor setting, or signs of active use to judge disposal. An intact jar/bottle indoors with no clear abandonment evidence is uncertain, not automatically litter and not proof the scene is clean. Do not use image failure or an unrecognized/ambiguous object to claim no litter.

Set outcome=litter_detected when any object is likely discarded; no_litter_detected only when you can confidently see no discarded waste (or all recognized items are clearly not discarded); otherwise use unable_to_determine. Empty objects only when no relevant object can be recognized. If it is too dark, blurry, distant, obstructed, or ambiguous, use unable_to_determine with low confidence and explain. Never claim no litter solely because recognition failed. Deduplicate repeated views of the same item.

For a confirmed report, give a short summary naming the visible waste. Be realistic about effort: one or two small items = difficulty 1, a scattered handful = 2, a larger area or several bags = 3, a substantial dump = 4-5. Estimate bags, minutes, and hazards from what is visible, not from the photo quality.
"""

CROP_PROMPT = """Recheck these photos for small or distant waste-capable objects. Full originals are followed by overlapping detail crops of the same photos. Identify specific object types first, then judge discard status from the full scene context. Do not count one physical item multiple times across crops. Return one consolidated assessment. Keep outcome unable_to_determine when recognition/context is insufficient; no_litter_detected requires confident visual evidence."""

VERIFY_PROMPT = """You are verifying a volunteer cleanup for a community cleanup app.
The first image(s) are BEFORE photos of a dirty spot. The LAST image is the AFTER photo the volunteer took.
Decide whether the AFTER photo shows the same place, and how much of the litter is gone.
Be fair but strict: a photo of a different clean place is NOT a cleanup.
"""


# ---------- scoring ----------

def points_for(difficulty: int, bags: int) -> int:
    difficulty = max(1, min(5, difficulty))
    bags = max(0, min(20, bags))
    return min(400, difficulty * difficulty * 10 + bags * 5)


def _parts(images: list[tuple[bytes, str]]):
    from google.genai import types
    return [types.Part.from_bytes(data=data, mime_type=mime) for data, mime in images]


def _generate(contents, schema):
    global ACTIVE_MODEL
    from google.genai import types
    def _config(model):
        extra = {}
        if "2.5" in model:  # thinking tokens are most of the cost; the photo checks don't need them
            extra["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        return types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.2,
            **extra,
        )
    try:
        resp = _get_client().models.generate_content(model=ACTIVE_MODEL, contents=contents, config=_config(ACTIVE_MODEL))
    except Exception as e:
        # 404 / "not found" / "not supported" = bad model name: switch to the fallback for good.
        msg = repr(e).lower()
        if ACTIVE_MODEL != FALLBACK_MODEL and ("404" in msg or "not found" in msg or "not supported" in msg):
            print(f"[ai] model {ACTIVE_MODEL!r} unavailable, falling back to {FALLBACK_MODEL!r}: {e!r}")
            ACTIVE_MODEL = FALLBACK_MODEL
            resp = _get_client().models.generate_content(model=ACTIVE_MODEL, contents=contents, config=_config(ACTIVE_MODEL))
        else:
            raise
    parsed = resp.parsed
    if parsed is None:
        parsed = schema.model_validate_json(resp.text)
    return parsed


def _detail_tiles(images):
    """Create four overlapping, native-resolution crops per image for small-object review."""
    from PIL import Image
    tiles = []
    for data, _mime in images:
        with Image.open(io.BytesIO(data)) as source:
            from PIL import ImageOps
            image = ImageOps.exif_transpose(source).convert("RGB")
            w, h = image.size
            # A 58% window with 42% stride gives overlap and covers edge objects.
            cw, ch = max(1, round(w * .58)), max(1, round(h * .58))
            xs = sorted(set((0, max(0, w - cw))))
            ys = sorted(set((0, max(0, h - ch))))
            for y in ys:
                for x in xs:
                    crop = image.crop((x, y, min(w, x + cw), min(h, y + ch)))
                    out = io.BytesIO()
                    crop.save(out, format="JPEG", quality=94)
                    tiles.append((out.getvalue(), "image/jpeg"))
    return tiles


def _apply_decision_thresholds(a):
    a.litter_confidence = max(0, min(100, a.litter_confidence))
    if a.outcome == "no_litter_detected" and a.litter_confidence < NO_LITTER_MIN_CONFIDENCE:
        a.outcome = "unable_to_determine"
    elif a.outcome == "litter_detected" and a.litter_confidence < LITTER_MIN_CONFIDENCE:
        a.outcome = "unable_to_determine"
    return a


def assess_spot(images: list[tuple[bytes, str]]) -> dict:
    """images: list of (bytes, mime). Returns dict with assessment + points + source."""
    client = _get_client()
    if client:
        try:
            a = _apply_decision_thresholds(_generate(_parts(images) + [ASSESS_PROMPT], Assessment))
            # Tile only when the full-image result is uncertain or misses all objects.
            if a.outcome == "unable_to_determine" or not a.objects:
                try:
                    tiles = _detail_tiles(images)
                    a = _apply_decision_thresholds(_generate(_parts(images + tiles) + [CROP_PROMPT], Assessment))
                except Exception as crop_error:
                    print(f"[ai] tiled review unavailable: {crop_error!r}")
            global LAST_ERROR
            LAST_ERROR = None
            a.difficulty = max(1, min(5, a.difficulty))
            a.estimated_bags = max(0, a.estimated_bags)
            for item in a.objects:
                item.count = max(1, item.count)
                item.object_confidence = max(0, min(100, item.object_confidence))
            data = a.model_dump()
            data["is_litter"] = a.outcome == "litter_detected"  # compatibility for older callers
            data["summary"] = data.pop("summary") or a.rationale
            points = points_for(a.difficulty, a.estimated_bags) if a.outcome == "litter_detected" else 0
            return {**data, "points": points, "source": "gemini"}
        except Exception as e:  # network, quota, billing... keep the demo alive
            LAST_ERROR = repr(e)[:300]
            print(f"[ai] assess failed, using demo mode: {e!r}")
    return {
        "outcome": "unable_to_determine",
        "objects": [],
        "rationale": "Photo analysis is unavailable. A volunteer should review the photo before this is treated as confirmed litter.",
        "is_litter": False,
        "litter_confidence": 0,
        "difficulty": 1,
        "estimated_bags": 0,
        "estimated_minutes": 0,
        "hazards": [],
        "summary": "AI could not determine whether this photo shows discarded litter.",
        "points": 0,
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
    return {"same_place": True, "percent_cleaned": 100,
            "comment": "Nice work! (demo mode: AI offline)", "source": "demo"}
