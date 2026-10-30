import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class UnknownUpload(StrictModel):
    image_base64: str = Field(max_length=350_000)
    client_revision: int = Field(default=0, ge=0)
    scene: str = Field(default="", max_length=64)
    client: str = Field(default="", max_length=64)


class UnknownBatch(StrictModel):
    candidates: list[UnknownUpload] = Field(min_length=1, max_length=50)


class ScreenshotReferenceBody(StrictModel):
    identity_id: str = Field(min_length=1, max_length=128)
    bounds: tuple[int, int, int, int]

    @field_validator("bounds", mode="before")
    @classmethod
    def validate_bounds(cls, value):
        if not isinstance(value, (list, tuple)) or len(value) != 4 or any(type(v) is not int for v in value):
            raise ValueError("Expected four integer coordinates")
        x1, y1, x2, y2 = value
        if not (0 <= x1 < x2 <= 1280 and 0 <= y1 < y2 <= 720):
            raise ValueError("Item bounds must lie inside the screenshot")
        return tuple(value)


class ScreenshotUpload(StrictModel):
    image_base64: str = Field(max_length=5_592_408)
    references: list[ScreenshotReferenceBody] = Field(min_length=1, max_length=100)
    scene: str = Field(max_length=64)
    client: str = Field(default="", max_length=64)


class MappingBody(StrictModel):
    item_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    tag: str = Field(default="", max_length=32)

    @field_validator("item_id")
    @classmethod
    def validate_id(cls, value):
        if value != value.strip() or not ITEM_ID.fullmatch(value) or value.startswith("U-"):
            raise ValueError("Use a stable formal item ID; U- is reserved for temporary identities")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        if not value.strip():
            raise ValueError("Display name cannot be blank")
        return value.strip()


class FormalUpload(MappingBody):
    image_base64: str = Field(max_length=350_000)


class PublishBody(StrictModel):
    note: str = Field(default="Administrator mapping publication", max_length=500)
