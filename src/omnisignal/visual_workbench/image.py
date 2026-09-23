"""Decode, bound and normalize local PNGs before storing them."""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from .contracts import VisualImage


MAX_UPLOAD_BYTES = 5_000_000
MAX_STORED_BYTES = 10_000_000
MAX_PIXELS = 16_000_000


def normalize_png(raw: bytes, *, kind: str) -> tuple[bytes, VisualImage]:
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("PNG must be between 1 byte and 5 MB")
    try:
        with Image.open(BytesIO(raw)) as source:
            if source.format != "PNG" or getattr(source, "n_frames", 1) != 1:
                raise ValueError("only single-frame PNG is supported")
            width, height = source.size
            if not 1 <= width <= 4096 or not 1 <= height <= 4096 or width * height > MAX_PIXELS:
                raise ValueError("PNG dimensions exceed the 4096 px / 16 MP limit")
            source.load()
            clean = source.convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("PNG cannot be decoded safely") from exc

    output = BytesIO()
    clean.save(output, format="PNG", optimize=True)
    stored = output.getvalue()
    if len(stored) > MAX_STORED_BYTES:
        raise ValueError("normalized PNG exceeds 10 MB")
    return stored, VisualImage(
        kind=kind,
        width=width,
        height=height,
        original_sha256=sha256(raw).hexdigest(),
        stored_sha256=sha256(stored).hexdigest(),
        byte_length=len(stored),
    )
