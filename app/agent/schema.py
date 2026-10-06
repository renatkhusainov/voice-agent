"""Turns a Pydantic model into the (properties, required) pair Pipecat's
``FunctionSchema`` expects for a tool's parameters — the model is the single
source of truth for a tool's shape; nothing in app/agent/tools.py describes a
field a second time by hand.

Pydantic represents a nested submodel field as a ``$ref`` into a top-level
``$defs`` map (see ``BaseModel.model_json_schema()``). ``FunctionSchema`` has
nowhere to put a sibling ``$defs`` — it only carries ``properties`` and
``required`` — so a tool whose input model nests a submodel (``DateRange``,
say) would otherwise ship a dangling ``$ref`` no consumer could resolve.
``inline_refs`` resolves every ``$ref`` into the schema it points to before
the properties are used, so nesting in the model costs nothing downstream.
"""

from typing import Any

from pydantic import BaseModel

__all__ = ["inline_refs", "schema_from_model"]


def inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """Recursively replace every ``{"$ref": "#/$defs/X"}`` with X's own
    schema, inlined. A sibling key next to a ``$ref`` (Pydantic puts a
    field-level ``description`` there, for instance) overrides the resolved
    schema's own matching key, matching how Pydantic itself layers them.
    """
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            resolved = inline_refs(defs[name], defs)
            overlay = {k: inline_refs(v, defs) for k, v in node.items() if k != "$ref"}
            return {**resolved, **overlay}
        return {k: inline_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [inline_refs(v, defs) for v in node]
    return node


def schema_from_model(model: type[BaseModel]) -> tuple[dict[str, Any], list[str]]:
    """(properties, required) for `model`, generated straight from Pydantic.

    Edit the model and this follows automatically — there is no second copy
    of the schema anywhere to fall out of sync with it.
    """
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})
    resolved = inline_refs(raw, defs)
    return resolved.get("properties", {}), resolved.get("required", [])
