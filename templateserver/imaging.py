import base64
import binascii
import hashlib
import struct
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Icon:
    sha256: str
    png: bytes
    pixels: bytes
    phash: str
    mean: list[float]
    stddev: float


def decode_icon(encoded: str, *, allow_cell: bool = False) -> Icon:
    try:
        if len(encoded) > 350_000:
            raise ValueError("Icon exceeds size limit")
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Expected base64 PNG icon") from exc
    if len(raw) < 33 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        raise ValueError("Only PNG is accepted")
    dimensions = struct.unpack(">II", raw[16:24])
    if dimensions != (49, 49) and not (allow_cell and dimensions == (96, 96)):
        raise ValueError("Expected 49x49 icon (formal import also accepts 96x96 item cell)")
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or image.ndim != 3 or image.shape[2] not in (3, 4) or image.dtype != np.uint8:
        raise ValueError("Expected 8-bit RGB/RGBA PNG")
    if image.shape[2] == 4:
        if np.any(image[:, :, 3] != 255):
            raise ValueError("Transparent icons are not accepted; upload opaque RGB")
        image = image[:, :, :3]
    if dimensions == (96, 96):
        image = image[21:70, 40:89]
    # All hashes and comparisons refer to canonical RGB pixels, not PNG encodings.
    rgb = np.ascontiguousarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    pixels = rgb.tobytes()
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    dct = cv2.dct(cv2.resize(gray, (32, 32)).astype(np.float32))[:8, :8].flatten()
    median = np.median(dct[1:])
    bits = dct > median
    bits[0] = False
    phash = f"{sum(int(bit) << index for index, bit in enumerate(bits)):016x}"
    ok, png = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("PNG encoding failed")
    return Icon(
        hashlib.sha256(pixels).hexdigest(),
        png.tobytes(),
        pixels,
        phash,
        rgb.mean(axis=(0, 1)).tolist(),
        float(np.sqrt(rgb.var(axis=(0, 1)).mean())),
    )


THRESHOLDS = {"formal": 0.92, "cluster": 0.85, "margin": 0.03, "phash_distance": 6, "color": 30}


def similarity(left: bytes, right: bytes, left_stddev: float, right_stddev: float) -> float:
    query = np.frombuffer(left, np.uint8).reshape(49, 49, 3)
    candidate = np.frombuffer(right, np.uint8).reshape(49, 49, 3)
    if min(left_stddev, right_stddev) < 5:
        return 1 - float(np.sqrt(np.mean((query.astype(float) - candidate) ** 2))) / 255
    return float(cv2.matchTemplate(query, candidate, cv2.TM_CCOEFF_NORMED)[0, 0])


def decode_screenshot(encoded: str) -> tuple[str, bytes]:
    """Validate dimensions before decoding; preserve the original PNG bytes and resolution."""
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Expected base64 PNG screenshot") from exc
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Screenshot exceeds 4 MiB")
    if len(raw) < 33 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        raise ValueError("Only PNG screenshots are accepted")
    if struct.unpack(">II", raw[16:24]) != (1280, 720):
        raise ValueError("Expected a complete 1280x720 game screenshot")
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or image.shape != (720, 1280, 3) or image.dtype != np.uint8:
        raise ValueError("Expected an 8-bit RGB screenshot")
    return hashlib.sha256(raw).hexdigest(), raw


def match_icon(icon: Icon, rows: list[dict], thresholds: dict | None = None) -> dict | None:
    limits = thresholds or THRESHOLDS
    for kind in ("formal", "temporary"):
        scores: dict[str, tuple[float, dict]] = {}
        for row in rows:
            if row["kind"] != kind:
                continue
            if kind == "formal" and (int(row["phash"], 16) ^ int(icon.phash, 16)).bit_count() > limits["phash_distance"]:
                continue
            if kind == "formal" and np.max(np.abs(np.array(row["mean"]) - icon.mean)) > limits["color"]:
                continue
            score = similarity(icon.pixels, row["pixels"], icon.stddev, row["stddev"])
            # Multiple variants of one identity cannot compete with each other for the margin.
            key = row["canonical_id"]
            if key not in scores or score > scores[key][0]:
                scores[key] = (score, row)
        ranked = sorted(scores.values(), key=lambda pair: pair[0], reverse=True)
        threshold = limits["formal" if kind == "formal" else "cluster"]
        if ranked and ranked[0][0] >= threshold:
            runner_up = ranked[1][0] if len(ranked) > 1 else -1
            if kind == "temporary" or ranked[0][0] - runner_up >= limits["margin"]:
                return ranked[0][1]
    return None
