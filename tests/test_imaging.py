import base64

import cv2
import numpy as np
import pytest
from conftest import encoded_image

from templateserver.imaging import decode_icon, match_icon


def candidate(icon, key="U-TEST", kind="temporary"):
    return {
        "id": key,
        "canonical_id": key,
        "kind": kind,
        "pixels": icon.pixels,
        "phash": icon.phash,
        "mean": icon.mean,
        "stddev": icon.stddev,
    }


def test_opaque_rgb_size_and_canonical_hash():
    image = encoded_image()
    icon = decode_icon(image)
    assert decode_icon(base64.b64encode(icon.png).decode()).sha256 == icon.sha256
    for invalid in ["not base64", encoded_image(size=48), base64.b64encode(b"not png").decode()]:
        with pytest.raises(ValueError):
            decode_icon(invalid)


def test_96_cell_crop_compatible():
    icon = decode_icon(encoded_image())
    cell = np.zeros((96, 96, 3), dtype=np.uint8)
    cell[21:70, 40:89] = cv2.imdecode(np.frombuffer(icon.png, np.uint8), cv2.IMREAD_COLOR)
    encoded = base64.b64encode(cv2.imencode(".png", cell)[1]).decode()
    assert decode_icon(encoded, allow_cell=True).sha256 == icon.sha256


def test_brightness_variant_formal_priority_and_same_item_margin():
    original = decode_icon(encoded_image())
    query = decode_icon(encoded_image(brightness=3))
    formal = candidate(original, "Blueprint", "formal")
    assert match_icon(query, [candidate(original), formal]) == formal
    assert match_icon(query, [formal, formal.copy()]) is not None
    assert match_icon(query, [formal, candidate(original, "DifferentItem", "formal")]) is None


def test_colored_flat_icons_cannot_get_false_ncc_one():
    def flat(red):
        pixels = np.full((49, 49, 3), (0, 0, red), np.uint8)
        return decode_icon(base64.b64encode(cv2.imencode(".png", pixels)[1]).decode())

    original, different = flat(100), flat(125)
    assert original.stddev == 0
    assert match_icon(different, [candidate(original, kind="formal")]) is None
