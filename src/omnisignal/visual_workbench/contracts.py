"""Strict contracts for visual projects; these models do not perform I/O."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import json
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VisualWorkflow(StrEnum):
    FROM_SCRATCH = "from_scratch"
    REVERSE_REBUILD = "reverse_rebuild"


class UsageBasis(StrEnum):
    OWNED = "owned"
    LICENSED = "licensed"
    PUBLIC_REFERENCE = "public_reference"


class LayerKind(StrEnum):
    BACKGROUND = "background"
    ATMOSPHERE = "atmosphere"
    PRODUCT = "product"
    LOGO = "logo"
    HEADLINE = "headline"
    BODY_TEXT = "body_text"
    DECORATION = "decoration"
    MASK = "mask"
    EVIDENCE = "evidence"


class CanvasSpec(StrictModel):
    width: int = Field(default=1080, ge=256, le=4096)
    height: int = Field(default=1350, ge=256, le=4096)
    color_mode: str = "RGB"

    @field_validator("color_mode")
    @classmethod
    def supported_color_mode(cls, value: str) -> str:
        if value != "RGB":
            raise ValueError("only RGB is supported by the visual skeleton")
        return value


class SourceReference(StrictModel):
    usage_basis: UsageBasis
    platform: str = Field(default="", max_length=80)
    source_url: str = Field(default="", max_length=2048)
    note: str = Field(default="", max_length=240)

    @field_validator("platform", "note")
    @classmethod
    def clean_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if any(ord(char) < 32 for char in normalized):
            raise ValueError("source metadata contains control characters")
        return normalized

    @field_validator("source_url")
    @classmethod
    def safe_reference_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("source URL must be a public http(s) reference without credentials or fragment")
        return value

    @model_validator(mode="after")
    def require_traceable_reference(self) -> "SourceReference":
        if not any((self.platform, self.source_url, self.note)):
            raise ValueError("source declaration requires a platform, public URL, or note")
        return self


class VisualProjectCreate(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    workflow: VisualWorkflow
    category_keyword: str = Field(min_length=1, max_length=80)
    target_brand: str = Field(min_length=1, max_length=80)
    competitors: tuple[str, ...] = Field(default=(), max_length=10)
    canvas: CanvasSpec = Field(default_factory=CanvasSpec)
    source: SourceReference | None = None

    @field_validator("name", "category_keyword", "target_brand")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized or any(ord(char) < 32 for char in normalized):
            raise ValueError("project text is invalid")
        return normalized

    @field_validator("competitors")
    @classmethod
    def normalize_competitors(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(" ".join(value.split()) for value in values)
        if any(not value or len(value) > 80 for value in normalized):
            raise ValueError("competitor name is invalid")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("competitor names must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_scope(self) -> "VisualProjectCreate":
        if self.target_brand.casefold() in {value.casefold() for value in self.competitors}:
            raise ValueError("target brand cannot also be a competitor")
        if self.workflow == VisualWorkflow.REVERSE_REBUILD and self.source is None:
            raise ValueError("reverse rebuild requires a source declaration")
        encoded = json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > 16_000:
            raise ValueError("visual project definition is too large")
        return self


class LayerSpec(StrictModel):
    layer_id: str = Field(pattern=r"[a-z][a-z0-9_]{1,47}")
    name: str = Field(min_length=1, max_length=80)
    kind: LayerKind
    editable: bool
    status: str = Field(default="placeholder", pattern=r"placeholder|ready|reconstructed")
    provenance: str = Field(min_length=1, max_length=120)
    asset_file: str | None = Field(default=None, pattern=r"layers/[a-z][a-z0-9_]{1,47}\.png")
    asset_sha256: str | None = Field(default=None, pattern=r"[a-f0-9]{64}")


class VisualImage(StrictModel):
    kind: str = Field(pattern=r"demo|uploaded")
    width: int = Field(ge=1, le=4096)
    height: int = Field(ge=1, le=4096)
    original_sha256: str = Field(pattern=r"[a-f0-9]{64}")
    stored_sha256: str = Field(pattern=r"[a-f0-9]{64}")
    byte_length: int = Field(ge=1, le=10_000_000)


class ImageRegion(StrictModel):
    layer_id: str = Field(pattern=r"[a-z][a-z0-9_]{1,47}")
    x: int = Field(ge=0, le=4096)
    y: int = Field(ge=0, le=4096)
    width: int = Field(ge=1, le=4096)
    height: int = Field(ge=1, le=4096)
    basis: str = Field(pattern=r"synthetic_composition")


class VisualProject(StrictModel):
    schema_version: str = "1.0"
    project_id: str = Field(pattern=r"visual_[a-f0-9]{32}")
    created_at: datetime
    created_by: str = Field(min_length=2, max_length=64)
    status: str = Field(default="draft", pattern=r"draft|ready_for_adapter|completed")
    definition: VisualProjectCreate
    layers: tuple[LayerSpec, ...] = Field(min_length=1, max_length=20)
    image: VisualImage | None = None
    regions: tuple[ImageRegion, ...] = Field(default=(), max_length=20)
    analysis_status: str = "not_requested"
    generation_status: str = "not_requested"
    photoshop_status: str = "not_configured"


def default_layers(workflow: VisualWorkflow) -> tuple[LayerSpec, ...]:
    shared = (
        LayerSpec(layer_id="background", name="背景", kind=LayerKind.BACKGROUND,
                  editable=True, provenance="new_or_reconstructed"),
        LayerSpec(layer_id="atmosphere", name="光影与氛围", kind=LayerKind.ATMOSPHERE,
                  editable=True, provenance="generated_or_designed"),
        LayerSpec(layer_id="product", name="产品主体", kind=LayerKind.PRODUCT,
                  editable=True, provenance="target_brand_asset"),
        LayerSpec(layer_id="decoration", name="装饰元素", kind=LayerKind.DECORATION,
                  editable=True, provenance="generated_or_designed"),
        LayerSpec(layer_id="logo", name="品牌标志", kind=LayerKind.LOGO,
                  editable=True, provenance="target_brand_asset"),
        LayerSpec(layer_id="headline", name="主标题", kind=LayerKind.HEADLINE,
                  editable=True, provenance="editable_text"),
        LayerSpec(layer_id="body_text", name="副文案", kind=LayerKind.BODY_TEXT,
                  editable=True, provenance="editable_text"),
    )
    if workflow == VisualWorkflow.FROM_SCRATCH:
        return shared
    return shared + (
        LayerSpec(layer_id="rebuild_mask", name="重建区域蒙版", kind=LayerKind.MASK,
                  editable=True, provenance="derived_from_reference"),
        LayerSpec(layer_id="evidence", name="分析证据标注", kind=LayerKind.EVIDENCE,
                  editable=False, provenance="reference_analysis_only"),
    )


def visual_capabilities() -> dict[str, object]:
    return {
        "contract_version": "1.0",
        "image_generation": {
            "status": "not_configured",
            "provider": "openai",
            "models": ["gpt-image-2.5-sunburst", "gpt-image-2.5-flare"],
            "operations": ["generate", "edit", "masked_edit"],
            "automatic_calls": False,
        },
        "photoshop": {
            "status": "not_configured",
            "adapter": "uxp_mcp",
            "required_operations": [
                "create_document", "place_asset", "create_text_layer", "set_layer_order", "save_psd"
            ],
            "automatic_writes": False,
        },
        "collection": {
            "status": "not_connected",
            "automatic_collection": False,
            "scope": "explicit platform, keyword, time window, and bounded sample only",
        },
    }
