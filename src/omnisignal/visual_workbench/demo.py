"""Deterministic layered poster built around one fictional product cutout."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .contracts import ImageRegion


WIDTH = 1080
HEIGHT = 1350
PRODUCT_FILE = Path(__file__).with_name("assets") / "synthetic_iced_coffee_product.png"


@dataclass(frozen=True)
class DemoPoster:
    preview: bytes
    layers: dict[str, bytes]
    regions: tuple[ImageRegion, ...]


def make_demo_poster() -> DemoPoster:
    layers = {name: Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0)) for name in (
        "background", "atmosphere", "product", "decoration", "logo", "headline", "body_text"
    )}

    background = layers["background"]
    background.paste((18, 27, 43, 255), (0, 0, WIDTH, HEIGHT))
    atmosphere = ImageDraw.Draw(layers["atmosphere"])
    atmosphere.ellipse((330, 240, 1240, 1230), fill=(39, 64, 82, 190))
    atmosphere.ellipse((-340, 860, 460, 1630), fill=(31, 91, 103, 105))

    decoration = ImageDraw.Draw(layers["decoration"])
    decoration.ellipse((90, 840, 310, 1060), outline=(245, 173, 91, 210), width=5)
    decoration.arc((50, 820, 380, 1150), start=20, end=300, fill=(245, 173, 91, 170), width=3)
    decoration.line((70, 1195, 1010, 1195), fill=(228, 184, 122, 155), width=2)

    with Image.open(PRODUCT_FILE) as source:
        product = source.convert("RGBA").resize((620, 930), Image.Resampling.LANCZOS)
    layers["product"].alpha_composite(product, (390, 300))

    headline = ImageDraw.Draw(layers["headline"])
    headline.text((72, 115), "NOVA", font=ImageFont.load_default(size=108), fill=(251, 240, 216, 255))
    headline.text((72, 225), "BREW", font=ImageFont.load_default(size=108), fill=(251, 240, 216, 255))
    body = ImageDraw.Draw(layers["body_text"])
    body.text((75, 370), "COLD BREW / 01", font=ImageFont.load_default(size=35), fill=(241, 185, 116, 255))
    body.text((75, 1250), "A cooler coffee moment", font=ImageFont.load_default(size=31), fill=(251, 240, 216, 255))
    logo = ImageDraw.Draw(layers["logo"])
    logo.ellipse((905, 83, 995, 173), outline=(251, 240, 216, 255), width=4)
    logo.text((930, 103), "N", font=ImageFont.load_default(size=51), fill=(251, 240, 216, 255))

    composite = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    for layer in layers.values():
        composite = Image.alpha_composite(composite, layer)

    regions = (
        ImageRegion(layer_id="background", x=0, y=0, width=WIDTH, height=HEIGHT, basis="synthetic_composition"),
        ImageRegion(layer_id="atmosphere", x=330, y=240, width=750, height=990, basis="synthetic_composition"),
        ImageRegion(layer_id="decoration", x=50, y=820, width=330, height=330, basis="synthetic_composition"),
        ImageRegion(layer_id="product", x=390, y=300, width=620, height=930, basis="synthetic_composition"),
        ImageRegion(layer_id="logo", x=905, y=83, width=90, height=90, basis="synthetic_composition"),
        ImageRegion(layer_id="headline", x=72, y=115, width=400, height=230, basis="synthetic_composition"),
        ImageRegion(layer_id="body_text", x=75, y=370, width=480, height=910, basis="synthetic_composition"),
    )
    return DemoPoster(
        preview=_png_bytes(composite),
        layers={name: _png_bytes(layer) for name, layer in layers.items()},
        regions=regions,
    )


def _png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
