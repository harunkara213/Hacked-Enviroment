"""Perceptual photo fingerprints, to catch the same picture being uploaded twice.

A 256-bit difference hash (dHash) survives re-saving, resizing and re-compressing, so
reusing a photo (same shot as "before" and "after", one photo for several spots, someone
else's upload) is caught even after it has been re-encoded. Two separate shots of the same
place differ by far more than the threshold, so honest photos aren't flagged.
"""
import io

HASH_SIZE = 16           # 16x16 comparisons -> 256 bits
SAME_PHOTO_MAX_BITS = 8  # <= 8 of 256 bits different = the same photo


def dhash(data: bytes) -> str | None:
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as im:
            g = ImageOps.exif_transpose(im).convert("L").resize((HASH_SIZE + 1, HASH_SIZE), Image.LANCZOS)
            px = list(g.getdata())
    except Exception:
        return None  # undecodable image: skip the check rather than block the upload
    bits = 0
    for row in range(HASH_SIZE):
        for col in range(HASH_SIZE):
            left = px[row * (HASH_SIZE + 1) + col]
            right = px[row * (HASH_SIZE + 1) + col + 1]
            bits = (bits << 1) | (left > right)
    return f"{bits:064x}"


def distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def same_photo(a: str | None, b: str | None) -> bool:
    return bool(a and b) and distance(a, b) <= SAME_PHOTO_MAX_BITS
