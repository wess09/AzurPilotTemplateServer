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
