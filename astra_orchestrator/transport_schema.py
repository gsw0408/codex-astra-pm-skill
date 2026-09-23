"""Codex Structured Outputs transport compatibility.

The schemas bundled under :mod:`astra_orchestrator.schemas` are the canonical
contracts enforced by the internal validators.  Codex Structured Outputs
accepts a smaller JSON Schema subset, so this module derives a transport-only
schema and reverses the two representation changes it introduces:

* optional object properties are required and nullable on the wire; and
* arbitrary-key objects are encoded as deterministic ``[{"key", "value"}]``
  entry arrays because transport objects must set ``additionalProperties`` to
  ``false``.

The conversion deliberately weakens conditional constraints that the
Structured Outputs subset cannot express.  The original Python validators
remain the authoritative post-transport gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any


_UNSUPPORTED_KEYWORDS = frozenset({
    "allOf",
    "not",
    "if",
    "then",
    "else",
    "dependentRequired",
    "dependentSchemas",
})
_UNSUPPORTED_BOUNDS = frozenset({"minLength", "maxLength"})
_IGNORED_METADATA = frozenset({"$schema", "$id", "title"})


def codex_transport_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Return a non-mutating Structured Outputs schema derived from ``schema``.

    The root must remain an object.  Unsupported conditional/composition
    constraints are intentionally left to the canonical internal validator.
    """

    if not isinstance(schema, Mapping):
        raise ValueError("output schema must be a JSON object")
    source = deepcopy(dict(schema))
    if source.get("type") != "object":
        raise ValueError("Codex output schema root must have type object")
    converted = _convert_schema(source, root=True)
    if converted.get("type") != "object":
        raise ValueError("Codex output schema root must remain an object")
    return converted


def decode_codex_transport_output(
    value: Mapping[str, Any], source_schema: Mapping[str, Any]
) -> dict[str, Any]:
    """Decode a Codex transport result back to the canonical contract shape.

    Only transport-introduced null optionals are omitted.  A null value for a
    canonically required field is preserved, including
    ``stage_transition.to_stage``.  Unknown keys are also preserved so the
    authoritative validator can reject them rather than this layer hiding
    them.
    """

    if not isinstance(value, Mapping):
        raise ValueError("Codex transport output must be a JSON object")
    if not isinstance(source_schema, Mapping):
        raise ValueError("source schema must be a JSON object")
    decoded = _decode_value(dict(value), dict(source_schema), dict(source_schema))
    if not isinstance(decoded, dict):
        raise ValueError("decoded Codex output must be a JSON object")
    return decoded


def _convert_schema(schema: Mapping[str, Any], *, root: bool = False) -> dict[str, Any]:
    source = dict(schema)

    # ``allOf`` is unsupported.  The only structural use in the bundled
    # contracts composes a canonical $ref with an extra constraint; retain the
    # referenced shape and let the internal validator enforce the refinement.
    structural_ref = _all_of_ref(source.get("allOf"))
    output: dict[str, Any] = {}
    if structural_ref is not None and "$ref" not in source:
        output["$ref"] = structural_ref

    for key, raw in source.items():
        if (
            key in _UNSUPPORTED_KEYWORDS
            or key in _UNSUPPORTED_BOUNDS
            or key in _IGNORED_METADATA
        ):
            continue
        if key == "oneOf":
            branches = _schema_list(raw, "oneOf")
            existing = output.setdefault("anyOf", [])
            if not isinstance(existing, list):
                raise ValueError("schema anyOf must be an array")
            existing.extend(_convert_schema(branch) for branch in branches)
            continue
        if key == "anyOf":
            branches = _schema_list(raw, "anyOf")
            existing = output.setdefault("anyOf", [])
            if not isinstance(existing, list):
                raise ValueError("schema anyOf must be an array")
            existing.extend(_convert_schema(branch) for branch in branches)
            continue
        if key == "const":
            output["enum"] = [deepcopy(raw)]
            inferred = _enum_type([raw])
            if inferred is not None:
                output.setdefault("type", inferred)
            continue
        if key in {"properties", "$defs"}:
            if not isinstance(raw, Mapping):
                raise ValueError(f"schema {key} must be an object")
            output[key] = {
                str(name): _convert_schema(_schema_object(child, f"{key}.{name}"))
                for name, child in raw.items()
            }
            continue
        if key == "items":
            output[key] = _convert_schema(_schema_object(raw, "items"))
            continue
        if key == "additionalProperties":
            # Object handling below either closes the object or replaces a
            # schema-valued dynamic map with an entry-list representation.
            continue
        output[key] = deepcopy(raw)

    if "enum" in output and "type" not in output:
        inferred = _enum_type(output["enum"])
        if inferred is not None:
            output["type"] = inferred

    if not _is_object_schema(source):
        return output

    properties_raw = source.get("properties", {})
    if not isinstance(properties_raw, Mapping):
        raise ValueError("object schema properties must be an object")
    additional = source.get("additionalProperties", False)
    if isinstance(additional, Mapping):
        if root:
            raise ValueError("Codex output schema root cannot be an arbitrary-key object")
        if properties_raw:
            raise ValueError(
                "mixed fixed and arbitrary object properties are not transport-compatible"
            )
        return _dynamic_map_schema(_schema_object(additional, "additionalProperties"), source)

    properties = output.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("converted object properties must be an object")
    originally_required = source.get("required", [])
    if not isinstance(originally_required, list) or any(
        not isinstance(name, str) for name in originally_required
    ):
        raise ValueError("object schema required must be an array of strings")
    required_set = set(originally_required)
    for name in list(properties):
        if name not in required_set:
            properties[name] = _nullable(properties[name])
    output["type"] = "object"
    output["properties"] = properties
    output["required"] = list(properties)
    output["additionalProperties"] = False
    return output


def _dynamic_map_schema(
    value_schema: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": _convert_schema(value_schema),
            },
            "required": ["key", "value"],
            "additionalProperties": False,
        },
    }
    description = source.get("description")
    prefix = (
        "Transport encoding for an object with arbitrary string keys; emit one "
        "entry per key and do not repeat keys."
    )
    result["description"] = f"{description} {prefix}" if description else prefix
    return result


def _nullable(schema: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(schema))
    schema_type = result.get("type")
    if schema_type == "null" or (
        isinstance(schema_type, list) and "null" in schema_type
    ):
        return result
    if isinstance(result.get("anyOf"), list):
        if not any(_is_null_schema(branch) for branch in result["anyOf"]):
            result["anyOf"].append({"type": "null"})
        return result
    # Always use a union rather than only adding ``null`` to ``type``.  A
    # constrained schema such as ``{"type": "boolean", "enum": [true]}``
    # would otherwise still reject null because the enum remains in force.
    return {"anyOf": [result, {"type": "null"}]}


def _decode_value(value: Any, schema: Mapping[str, Any], root_schema: Mapping[str, Any]) -> Any:
    current = dict(schema)
    reference = current.pop("$ref", None)
    if reference is not None:
        value = _decode_value(value, _resolve_ref(reference, root_schema), root_schema)
        if not current:
            return value

    all_of = current.pop("allOf", None)
    if isinstance(all_of, list):
        for branch in all_of:
            if not isinstance(branch, Mapping) or _is_conditional(branch):
                continue
            value = _decode_value(value, branch, root_schema)

    for keyword in ("oneOf", "anyOf"):
        branches = current.get(keyword)
        if isinstance(branches, list) and branches:
            selected = _select_branch(value, branches, root_schema)
            value = _decode_value(value, selected, root_schema)
            break

    if _is_dynamic_map(current):
        value_schema = _schema_object(
            current["additionalProperties"], "additionalProperties"
        )
        if isinstance(value, Mapping):
            return {
                str(key): _decode_value(item, value_schema, root_schema)
                for key, item in value.items()
            }
        if not isinstance(value, list):
            return value
        decoded: dict[str, Any] = {}
        for index, entry in enumerate(value):
            if not isinstance(entry, Mapping):
                raise ValueError(f"dynamic-map entry {index} must be an object")
            if set(entry) != {"key", "value"} or not isinstance(entry["key"], str):
                raise ValueError(
                    f"dynamic-map entry {index} must contain string key and value"
                )
            key = entry["key"]
            if key in decoded:
                raise ValueError(f"dynamic-map transport contains duplicate key: {key}")
            decoded[key] = _decode_value(entry["value"], value_schema, root_schema)
        return decoded

    properties = current.get("properties")
    if isinstance(value, Mapping) and isinstance(properties, Mapping):
        required = current.get("required", [])
        required_set = set(required) if isinstance(required, list) else set()
        decoded_object: dict[str, Any] = {}
        for key, item in value.items():
            child_schema = properties.get(key)
            if child_schema is None:
                # Preserve unknown keys so canonical validation rejects them.
                decoded_object[str(key)] = item
                continue
            if item is None and key not in required_set:
                continue
            decoded_object[str(key)] = _decode_value(
                item, _schema_object(child_schema, f"properties.{key}"), root_schema
            )
        return decoded_object

    items = current.get("items")
    if isinstance(value, list) and isinstance(items, Mapping):
        return [_decode_value(item, items, root_schema) for item in value]
    return value


def _resolve_ref(reference: Any, root_schema: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise ValueError(f"only local JSON Schema references are supported: {reference!r}")
    current: Any = root_schema
    for raw in reference[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or token not in current:
            raise ValueError(f"unresolvable local JSON Schema reference: {reference}")
        current = current[token]
    return _schema_object(current, reference)


def _select_branch(
    value: Any, branches: list[Any], root_schema: Mapping[str, Any]
) -> Mapping[str, Any]:
    candidates = [
        _schema_object(branch, "union branch")
        for branch in branches
        if isinstance(branch, Mapping)
    ]
    for branch in candidates:
        if _matches_shape(value, branch, root_schema):
            return branch
    return candidates[0] if candidates else {}


def _matches_shape(
    value: Any, schema: Mapping[str, Any], root_schema: Mapping[str, Any]
) -> bool:
    if "$ref" in schema:
        return _matches_shape(value, _resolve_ref(schema["$ref"], root_schema), root_schema)
    schema_type = schema.get("type")
    if schema_type == "null":
        return value is None
    if schema_type == "object":
        if _is_dynamic_map(schema):
            return isinstance(value, (Mapping, list))
        return isinstance(value, Mapping)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if isinstance(schema_type, list):
        return any(
            _matches_shape(value, {"type": item}, root_schema) for item in schema_type
        )
    if "properties" in schema:
        return isinstance(value, Mapping)
    if "enum" in schema:
        return value in schema["enum"]
    if "const" in schema:
        return value == schema["const"]
    return True


def _all_of_ref(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    for branch in value:
        if isinstance(branch, Mapping) and set(branch) == {"$ref"}:
            reference = branch["$ref"]
            if not isinstance(reference, str):
                raise ValueError("schema $ref must be a string")
            return reference
    return None


def _is_conditional(schema: Mapping[str, Any]) -> bool:
    return any(key in schema for key in ("if", "then", "else", "not"))


def _is_object_schema(schema: Mapping[str, Any]) -> bool:
    schema_type = schema.get("type")
    return (
        schema_type == "object"
        or "properties" in schema
        or "additionalProperties" in schema
    )


def _is_dynamic_map(schema: Mapping[str, Any]) -> bool:
    return (
        schema.get("type") == "object"
        and isinstance(schema.get("additionalProperties"), Mapping)
        and not schema.get("properties")
    )


def _is_null_schema(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("type") == "null"


def _enum_type(value: Any) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    if all(isinstance(item, str) for item in value):
        return "string"
    if all(isinstance(item, bool) for item in value):
        return "boolean"
    if all(isinstance(item, int) and not isinstance(item, bool) for item in value):
        return "integer"
    if all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
        return "number"
    return None


def _schema_list(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"schema {label} must be a non-empty array")
    return [_schema_object(item, f"{label} branch") for item in value]


def _schema_object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"schema {label} must be an object")
    return value
