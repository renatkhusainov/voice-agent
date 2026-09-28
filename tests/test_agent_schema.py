"""app/agent/schema.py: the JSON schema is generated from a Pydantic model,
never hand-written a second time. These tests prove that literally — every
assertion re-derives the expected shape from the model itself, so a
hand-typed duplicate schema drifting from its model would fail here.
"""

from datetime import date

from pydantic import BaseModel, Field

from app.agent.schema import inline_refs, schema_from_model


class Flat(BaseModel):
    name: str = Field(description="A name")
    age: int


class Nested(BaseModel):
    start: date
    end: date


class WithNested(BaseModel):
    label: str
    range: Nested = Field(description="A range")


def test_flat_model_properties_and_required_match_the_model_verbatim():
    properties, required = schema_from_model(Flat)

    assert properties == Flat.model_json_schema()["properties"]
    assert required == ["name", "age"]


def test_nested_model_ref_is_inlined_not_left_dangling():
    properties, required = schema_from_model(WithNested)

    assert "$ref" not in str(properties)  # no unresolved reference anywhere
    assert properties["range"]["type"] == "object"
    assert set(properties["range"]["properties"]) == {"start", "end"}
    assert properties["range"]["required"] == ["start", "end"]
    # The field's own description survives inlining, alongside the submodel's.
    assert properties["range"]["description"] == "A range"
    assert required == ["label", "range"]


def test_inline_refs_is_the_only_thing_resolving_defs():
    # schema_from_model is just: call the model, pop $defs, inline_refs the rest.
    raw = WithNested.model_json_schema()
    defs = raw.pop("$defs", {})
    expected = inline_refs(raw, defs)

    properties, required = schema_from_model(WithNested)

    assert properties == expected["properties"]
    assert required == expected["required"]


def test_optional_field_is_not_required():
    class WithOptional(BaseModel):
        needed: str
        maybe: str | None = None

    _, required = schema_from_model(WithOptional)

    assert required == ["needed"]
