from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from mcp_metabase.models import ObjectType, PatchOperation, PlannedMutation


class MutationValidationError(RuntimeError):
    """Raised when a requested mutation is not closed, bounded, or preservation-safe."""


STATE_FIELDS: dict[ObjectType, tuple[str, ...]] = {
    ObjectType.QUESTION: (
        "id",
        "entity_id",
        "name",
        "description",
        "display",
        "dataset_query",
        "parameters",
        "parameter_mappings",
        "visualization_settings",
        "archived",
        "collection_id",
        "collection_position",
        "cache_ttl",
        "type",
        "dashboard_id",
        "dashboard_tab_id",
        "result_metadata",
        "updated_at",
        "last-edit-info",
    ),
    ObjectType.DASHBOARD: (
        "id",
        "entity_id",
        "name",
        "description",
        "parameters",
        "tabs",
        "dashcards",
        "archived",
        "collection_id",
        "collection_position",
        "cache_ttl",
        "width",
        "caveats",
        "points_of_interest",
        "show_in_getting_started",
        "updated_at",
        "last-edit-info",
    ),
    ObjectType.COLLECTION: (
        "id",
        "entity_id",
        "name",
        "description",
        "parent_id",
        "namespace",
        "authority_level",
        "archived",
        "location",
        "updated_at",
    ),
    ObjectType.FIELD: (
        "id",
        "name",
        "display_name",
        "description",
        "caveats",
        "points_of_interest",
        "semantic_type",
        "coercion_strategy",
        "fk_target_field_id",
        "visibility_type",
        "has_field_values",
        "settings",
        "nfc_path",
        "json_unfolding",
        "table_id",
        "database_id",
        "updated_at",
    ),
    ObjectType.DATABASE: ("id", "name", "engine", "initial_sync_status", "updated_at"),
}

MUTABLE_ROOTS: dict[ObjectType, frozenset[str]] = {
    ObjectType.QUESTION: frozenset(
        {
            "name",
            "description",
            "display",
            "dataset_query",
            "parameters",
            "parameter_mappings",
            "visualization_settings",
            "archived",
            "collection_id",
            "collection_position",
            "cache_ttl",
            "type",
            "result_metadata",
        }
    ),
    ObjectType.DASHBOARD: frozenset(
        {
            "name",
            "description",
            "parameters",
            "tabs",
            "dashcards",
            "archived",
            "collection_id",
            "collection_position",
            "cache_ttl",
            "width",
            "caveats",
            "points_of_interest",
            "show_in_getting_started",
        }
    ),
    ObjectType.COLLECTION: frozenset(
        {"name", "description", "parent_id", "authority_level", "archived"}
    ),
    ObjectType.FIELD: frozenset(
        {
            "display_name",
            "description",
            "caveats",
            "points_of_interest",
            "semantic_type",
            "coercion_strategy",
            "fk_target_field_id",
            "visibility_type",
            "has_field_values",
            "settings",
            "nfc_path",
            "json_unfolding",
        }
    ),
}

PROTECTED_ROOTS: dict[ObjectType, frozenset[str]] = {
    ObjectType.QUESTION: frozenset(
        {"dataset_query", "parameters", "parameter_mappings", "visualization_settings"}
    ),
    ObjectType.DASHBOARD: frozenset({"parameters", "tabs", "dashcards"}),
    ObjectType.COLLECTION: frozenset(),
    ObjectType.FIELD: frozenset({"settings"}),
}

ARRAY_ROOTS: dict[ObjectType, frozenset[str]] = {
    ObjectType.QUESTION: frozenset({"parameters", "parameter_mappings", "result_metadata"}),
    ObjectType.DASHBOARD: frozenset({"parameters", "tabs", "dashcards"}),
    ObjectType.COLLECTION: frozenset(),
    ObjectType.FIELD: frozenset({"nfc_path"}),
}

DASHBOARD_COUPLED_WRITE_ROOTS = ("tabs", "dashcards")

EDIT_SESSION_SCALAR_ROOTS: dict[ObjectType, frozenset[str]] = {
    ObjectType.QUESTION: frozenset({"name", "description", "display"}),
    ObjectType.DASHBOARD: frozenset({"name", "description", "width"}),
}

EDIT_SESSION_DASHCARD_GEOMETRY = frozenset({"row", "col", "size_x", "size_y", "dashboard_tab_id"})

EDIT_SESSION_FORBIDDEN_VISUALIZATION_KEYS = frozenset(
    {
        "click",
        "clickbehavior",
        "clicklinktemplate",
        "linktemplate",
        "linktext",
        "linktexttemplate",
        "linktype",
        "parametermapping",
        "parametermappings",
        "targetid",
    }
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _semantic_subset_matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _semantic_subset_matches(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(
                _semantic_subset_matches(left, right)
                for left, right in zip(expected, actual, strict=True)
            )
        )
    return expected == actual


def _native_template_tag_signature(raw_tags: Any) -> list[dict[str, Any]] | None:
    if isinstance(raw_tags, dict):
        source = raw_tags.items()
    elif isinstance(raw_tags, list):
        if not all(isinstance(tag, dict) for tag in raw_tags):
            return None
        source = ((tag.get("name"), tag) for tag in raw_tags)
    else:
        return None

    tags: dict[str, dict[str, Any]] = {}
    for fallback_name, raw_tag in source:
        if not isinstance(fallback_name, str) or not fallback_name or not isinstance(raw_tag, dict):
            return None
        tag = copy.deepcopy(raw_tag)
        tag_name = tag.get("name", fallback_name)
        if not isinstance(tag_name, str) or not tag_name or tag_name in tags:
            return None
        tag["name"] = tag_name
        tags[tag_name] = tag
    return [tags[name] for name in sorted(tags)]


def _native_dataset_query_signature(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    database = value.get("database")
    ignored_top_level = {
        "constraints",
        "info",
        "lib.convert/converted?",
        "middleware",
        "parameters",
        "pretty",
    }
    if value.get("type") == "native":
        native = value.get("native")
        if not isinstance(native, dict) or not isinstance(native.get("query"), str):
            return None
        tags = _native_template_tag_signature(native.get("template-tags", {}))
        if tags is None:
            return None
        return {
            "database": database,
            "query": native["query"],
            "template-tags": tags,
            "native-options": {
                key: copy.deepcopy(item)
                for key, item in native.items()
                if key not in {"query", "template-tags"}
            },
            "top-level-options": {
                key: copy.deepcopy(item)
                for key, item in value.items()
                if key not in {"type", "database", "native"} | ignored_top_level
            },
        }

    stages = value.get("stages")
    if (
        value.get("lib/type") != "mbql/query"
        or not isinstance(stages, list)
        or len(stages) != 1
        or not isinstance(stages[0], dict)
        or stages[0].get("lib/type") != "mbql.stage/native"
        or not isinstance(stages[0].get("native"), str)
    ):
        return None
    stage = stages[0]
    tags = _native_template_tag_signature(stage.get("template-tags", []))
    if tags is None:
        return None
    return {
        "database": database,
        "query": stage["native"],
        "template-tags": tags,
        "native-options": {
            key: copy.deepcopy(item)
            for key, item in stage.items()
            if key not in {"lib/type", "lib/uuid", "native", "template-tags"}
        },
        "top-level-options": {
            key: copy.deepcopy(item)
            for key, item in value.items()
            if key not in {"lib/type", "database", "stages"} | ignored_top_level
        },
    }


def dataset_query_semantically_matches(expected: Any, actual: Any) -> bool:
    """Compare accepted legacy and MBQL v2 native queries by persisted semantics."""

    expected_signature = _native_dataset_query_signature(expected)
    actual_signature = _native_dataset_query_signature(actual)
    if expected_signature is None or actual_signature is None:
        return canonical_sha256(expected) == canonical_sha256(actual)
    return _semantic_subset_matches(expected_signature, actual_signature)


def project_state(raw: dict[str, Any], object_type: ObjectType) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MutationValidationError("Metabase object state must be a JSON object.")
    state = {
        field: copy.deepcopy(raw[field]) for field in STATE_FIELDS[object_type] if field in raw
    }
    if type(state.get("id")) is not int or state["id"] <= 0:
        raise MutationValidationError("Metabase object has no positive immutable id.")
    return state


def _decode_pointer(path: str) -> list[str]:
    if not path.startswith("/") or path == "/" or len(path) > 512:
        raise MutationValidationError("Patch path must be one non-root JSON Pointer.")
    tokens: list[str] = []
    for raw in path[1:].split("/"):
        index = 0
        decoded = ""
        while index < len(raw):
            if raw[index] != "~":
                decoded += raw[index]
                index += 1
                continue
            if index + 1 >= len(raw) or raw[index + 1] not in {"0", "1"}:
                raise MutationValidationError("Patch path contains invalid JSON Pointer escaping.")
            decoded += "~" if raw[index + 1] == "0" else "/"
            index += 2
        if not decoded or len(decoded) > 200:
            raise MutationValidationError("Patch path contains an empty or oversized segment.")
        tokens.append(decoded)
    if len(tokens) > 16:
        raise MutationValidationError("Patch path is too deep.")
    return tokens


def _validate_dashboard_geometry(key: str, value: Any) -> None:
    if key in {"row", "col"}:
        if type(value) is not int or not 0 <= value <= 10_000:
            raise MutationValidationError(
                "Dashboard row/col must be a bounded non-negative integer."
            )
    elif key == "size_x":
        if type(value) is not int or not 1 <= value <= 24:
            raise MutationValidationError("Dashboard size_x must be an integer from 1 to 24.")
    elif key == "size_y":
        if type(value) is not int or not 1 <= value <= 1_000:
            raise MutationValidationError("Dashboard size_y must be a bounded positive integer.")
    elif key == "dashboard_tab_id" and value is not None and (type(value) is not int or value <= 0):
        raise MutationValidationError("Dashboard tab id must be a positive integer or null.")


def _visualization_key_marker(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _contains_forbidden_visualization_behavior(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                continue
            marker = _visualization_key_marker(key)
            if marker in EDIT_SESSION_FORBIDDEN_VISUALIZATION_KEYS:
                return True
            if marker == "viewas" and isinstance(nested, str) and nested.casefold() == "link":
                return True
            if _contains_forbidden_visualization_behavior(nested):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_forbidden_visualization_behavior(item) for item in value)
    return False


def _validate_edit_session_visualization_operation(
    tokens: list[str],
    operation: PatchOperation,
) -> None:
    markers = {_visualization_key_marker(token) for token in tokens[1:]}
    if markers & EDIT_SESSION_FORBIDDEN_VISUALIZATION_KEYS:
        raise MutationValidationError(
            "Click, link, and parameter-mapping behavior requires a prompt-gated exact update."
        )
    if (
        tokens[1:]
        and _visualization_key_marker(tokens[-1]) == "viewas"
        and isinstance(operation.value, str)
        and operation.value.casefold() == "link"
    ):
        raise MutationValidationError(
            "Click, link, and parameter-mapping behavior requires a prompt-gated exact update."
        )
    if _contains_forbidden_visualization_behavior(operation.value):
        raise MutationValidationError(
            "Click, link, and parameter-mapping behavior requires a prompt-gated exact update."
        )


def _validate_edit_session_scalar(root: str, value: Any) -> None:
    if root in {"name", "display"} and (not isinstance(value, str) or not value.strip()):
        raise MutationValidationError(f"Edit-session {root} must be a non-empty string.")
    if root == "description" and value is not None and not isinstance(value, str):
        raise MutationValidationError("Edit-session description must be a string or null.")
    if root == "width" and value not in {"fixed", "full"}:
        raise MutationValidationError("Dashboard width must be fixed or full.")


def validate_edit_session_operations(
    object_type: ObjectType,
    operations: list[PatchOperation],
) -> None:
    """Restrict auto-approved edit-session writes to presentation/layout paths."""

    if object_type not in {ObjectType.QUESTION, ObjectType.DASHBOARD}:
        raise MutationValidationError("Edit sessions support only questions and dashboards.")
    for operation in operations:
        tokens = _decode_pointer(operation.path)
        root = tokens[0]
        if root in EDIT_SESSION_SCALAR_ROOTS[object_type]:
            if len(tokens) != 1 or operation.op != "set":
                raise MutationValidationError(
                    "Edit-session metadata fields require one top-level set operation."
                )
            _validate_edit_session_scalar(root, operation.value)
            continue

        if object_type is ObjectType.QUESTION:
            if root != "visualization_settings" or operation.op not in {
                "set",
                "remove",
                "replace_array",
            }:
                raise MutationValidationError(
                    "Question edit sessions allow only presentation settings and display metadata."
                )
            if operation.op == "remove" and len(tokens) == 1:
                raise MutationValidationError(
                    "Question visualization_settings cannot be removed as a whole."
                )
            _validate_edit_session_visualization_operation(tokens, operation)
            continue

        if root == "tabs":
            if (
                operation.op != "dashboard_item_set"
                or type(operation.item_id) is not int
                or operation.item_id <= 0
                or _decode_pointer(str(operation.item_path)) != ["name"]
                or not isinstance(operation.value, str)
                or not operation.value.strip()
            ):
                raise MutationValidationError(
                    "Dashboard edit sessions may only rename an existing tab."
                )
            continue

        if root != "dashcards" or operation.op not in {
            "dashboard_item_set",
            "dashboard_item_remove",
            "dashboard_item_replace_array",
        }:
            raise MutationValidationError(
                "Dashboard edit sessions allow only existing-item layout and presentation changes."
            )
        if type(operation.item_id) is not int or operation.item_id <= 0:
            raise MutationValidationError(
                "Dashboard edit sessions require one positive existing item id."
            )
        item_tokens = _decode_pointer(str(operation.item_path))
        item_root = item_tokens[0]
        if item_root == "visualization_settings":
            if operation.op == "dashboard_item_remove" and len(item_tokens) == 1:
                raise MutationValidationError(
                    "Dashcard visualization_settings cannot be removed as a whole."
                )
            _validate_edit_session_visualization_operation(item_tokens, operation)
            continue
        if (
            item_root not in EDIT_SESSION_DASHCARD_GEOMETRY
            or len(item_tokens) != 1
            or operation.op != "dashboard_item_set"
        ):
            raise MutationValidationError(
                "Dashboard edit sessions cannot change card identity, mappings, or composition."
            )
        _validate_dashboard_geometry(item_root, operation.value)


def validate_edit_session_state_bindings(
    object_type: ObjectType,
    operations: list[PatchOperation],
    current_state: dict[str, Any],
) -> None:
    """Bind layout moves to tabs already present in the leased dashboard state."""

    if object_type is not ObjectType.DASHBOARD:
        return
    tabs = current_state.get("tabs")
    if not isinstance(tabs, list):
        raise MutationValidationError("Dashboard tabs are unavailable for edit-session binding.")
    tab_ids = {
        tab["id"]
        for tab in tabs
        if isinstance(tab, dict) and type(tab.get("id")) is int and tab["id"] > 0
    }
    for operation in operations:
        if operation.path != "/dashcards" or operation.op != "dashboard_item_set":
            continue
        if _decode_pointer(str(operation.item_path)) != ["dashboard_tab_id"]:
            continue
        if operation.value is not None and operation.value not in tab_ids:
            raise MutationValidationError(
                "Dashboard edit-session moves may target only an existing dashboard tab."
            )


def _parent_dict(document: dict[str, Any], tokens: list[str]) -> tuple[dict[str, Any], str]:
    current: Any = document
    for token in tokens[:-1]:
        if not isinstance(current, dict) or token not in current:
            raise MutationValidationError("Patch parent path does not exist as an object.")
        current = current[token]
        if isinstance(current, list):
            raise MutationValidationError("Array indexes are forbidden; replace the full array.")
    if not isinstance(current, dict):
        raise MutationValidationError("Patch parent is not an object.")
    return current, tokens[-1]


def _dashboard_item(document: dict[str, Any], root: str, item_id: int | str) -> dict[str, Any]:
    if root not in {"dashcards", "tabs", "parameters"}:
        raise MutationValidationError("Dashboard item operations support only closed array roots.")
    items = document.get(root)
    if not isinstance(items, list):
        raise MutationValidationError("Dashboard item array is unavailable.")
    matches = [item for item in items if isinstance(item, dict) and item.get("id") == item_id]
    if len(matches) != 1:
        raise MutationValidationError("Dashboard item id must match exactly one array item.")
    return matches[0]


def _apply_operation(
    document: dict[str, Any], object_type: ObjectType, operation: PatchOperation
) -> str:
    tokens = _decode_pointer(operation.path)
    root = tokens[0]
    if root not in MUTABLE_ROOTS.get(object_type, frozenset()):
        raise MutationValidationError("Patch path is outside the object-type allowlist.")

    if operation.op in {
        "dashboard_item_set",
        "dashboard_item_remove",
        "dashboard_item_replace_array",
    }:
        if object_type is not ObjectType.DASHBOARD or len(tokens) != 1:
            raise MutationValidationError("Dashboard item operation has an invalid root binding.")
        item = _dashboard_item(document, root, operation.item_id)
        item_tokens = _decode_pointer(str(operation.item_path))
        parent, key = _parent_dict(item, item_tokens)
        if operation.op == "dashboard_item_remove":
            if key not in parent:
                raise MutationValidationError("Dashboard item remove target does not exist.")
            del parent[key]
        else:
            if operation.op == "dashboard_item_replace_array":
                if key in parent and not isinstance(parent[key], list):
                    raise MutationValidationError(
                        "Dashboard item replace_array target is not an array."
                    )
            elif isinstance(operation.value, list):
                raise MutationValidationError(
                    "Dashboard item set cannot replace arrays; use dashboard_item_replace_array."
                )
            parent[key] = copy.deepcopy(operation.value)
        return root

    parent, key = _parent_dict(document, tokens)
    if operation.op == "remove":
        if len(tokens) == 1:
            raise MutationValidationError("Removing a top-level field is ambiguous and forbidden.")
        if key not in parent:
            raise MutationValidationError("Patch remove target does not exist.")
        del parent[key]
    elif operation.op == "replace_array":
        if len(tokens) == 1 and root not in ARRAY_ROOTS.get(object_type, frozenset()):
            raise MutationValidationError("This top-level field is not an array contract.")
        if key in parent and not isinstance(parent[key], list):
            raise MutationValidationError("Patch replace_array target is not an array.")
        parent[key] = copy.deepcopy(operation.value)
    elif operation.op == "set":
        if isinstance(operation.value, list):
            raise MutationValidationError("Array values require replace_array.")
        parent[key] = copy.deepcopy(operation.value)
    else:  # pragma: no cover - Pydantic keeps this enum closed.
        raise MutationValidationError("Unsupported patch operation.")
    return root


def _positive_or_none(value: Any, label: str) -> None:
    if value is not None and (type(value) is not int or value <= 0):
        raise MutationValidationError(f"{label} must be a positive integer or null.")


def _parameter_family(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.casefold()
    if normalized.startswith(("date", "time")) or "date" in normalized:
        return "date"
    if normalized.startswith("location/") or normalized in {"latitude", "longitude"}:
        return "location"
    if normalized.startswith(("number", "id")) or normalized in {"numeric", "integer", "float"}:
        return "number"
    if normalized.startswith(("string", "category")) or normalized in {"text", "keyword"}:
        return "string"
    return None


def _template_tag_name(target: Any) -> str | None:
    if not isinstance(target, list):
        return None
    for item in target:
        if (
            isinstance(item, list)
            and len(item) >= 2
            and item[0] == "template-tag"
            and isinstance(item[1], str)
        ):
            return item[1]
        nested = _template_tag_name(item)
        if nested:
            return nested
    return None


def _template_tags_by_name(raw_tags: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw_tags, dict):
        return {
            name: tag
            for name, tag in raw_tags.items()
            if isinstance(name, str) and isinstance(tag, dict)
        }
    if isinstance(raw_tags, list):
        return {
            tag["name"]: tag
            for tag in raw_tags
            if isinstance(tag, dict) and isinstance(tag.get("name"), str) and tag["name"].strip()
        }
    return {}


def _card_native_template_tags(card: Any) -> dict[str, tuple[dict[str, Any], int | None]]:
    if not isinstance(card, dict):
        return {}
    dataset_query = card.get("dataset_query")
    if not isinstance(dataset_query, dict):
        return {}

    tags: dict[str, tuple[dict[str, Any], int | None]] = {}
    native = dataset_query.get("native")
    if isinstance(native, dict):
        tags.update(
            (name, (tag, None))
            for name, tag in _template_tags_by_name(native.get("template-tags")).items()
        )

    stages = dataset_query.get("stages")
    if isinstance(stages, list):
        for stage_number, stage in enumerate(stages):
            if isinstance(stage, dict) and stage.get("lib/type") == "mbql.stage/native":
                for name, tag in _template_tags_by_name(stage.get("template-tags")).items():
                    if name in tags:
                        raise MutationValidationError(
                            "Native template-tag names must be unique across query stages."
                        )
                    tags[name] = (tag, stage_number)
    return tags


def _target_stage_numbers(target: Any) -> list[Any]:
    if isinstance(target, dict):
        values = [target.get("stage-number")] if "stage-number" in target else []
        for value in target.values():
            values.extend(_target_stage_numbers(value))
        return values
    if isinstance(target, list):
        values: list[Any] = []
        for value in target:
            values.extend(_target_stage_numbers(value))
        return values
    return []


def _template_tag_target_kind(target: Any) -> str | None:
    if isinstance(target, list) and target and target[0] in {"dimension", "variable"}:
        return target[0]
    return None


def _dashboard_validation_state(
    state: dict[str, Any],
    cards: dict[int, dict[str, Any]] | None,
) -> dict[str, Any]:
    candidate = copy.deepcopy(state)
    if not cards:
        return candidate
    for dashcard in candidate.get("dashcards", []) or []:
        if not isinstance(dashcard, dict):
            continue
        card_id = dashcard.get("card_id")
        if type(card_id) is int and card_id in cards:
            dashcard["card"] = copy.deepcopy(cards[card_id])
    return candidate


def canonicalize_dashboard_parameter_mappings(
    state: dict[str, Any],
    *,
    cards: dict[int, dict[str, Any]] | None = None,
) -> None:
    """Fill the executable native-mapping fields Metabase otherwise accepts silently."""

    candidate = _dashboard_validation_state(state, cards)
    source_dashcards = state.get("dashcards", []) or []
    candidate_dashcards = candidate.get("dashcards", []) or []
    for dashcard, hydrated in zip(source_dashcards, candidate_dashcards, strict=True):
        if not isinstance(dashcard, dict) or not isinstance(hydrated, dict):
            continue
        card_id = dashcard.get("card_id")
        tags = _card_native_template_tags(hydrated.get("card"))
        for mapping in dashcard.get("parameter_mappings", []) or []:
            if not isinstance(mapping, dict):
                continue
            target = mapping.get("target")
            tag_name = _template_tag_name(target)
            if not tag_name:
                continue
            if type(card_id) is not int or card_id <= 0:
                raise MutationValidationError(
                    "Native dashboard mappings require a positive dashcard card_id."
                )
            mapped_card_id = mapping.get("card_id")
            if mapped_card_id is None:
                mapping["card_id"] = card_id
            elif mapped_card_id != card_id:
                raise MutationValidationError(
                    "Dashboard mapping card_id must match its owning dashcard."
                )
            tag = tags.get(tag_name)
            if tag is None:
                raise MutationValidationError(
                    "Dashboard mapping references a missing native template tag."
                )
            _, stage_number = tag
            if stage_number is None:
                continue
            target_kind = _template_tag_target_kind(target)
            stage_numbers = _target_stage_numbers(target)
            if target_kind == "variable":
                if stage_numbers:
                    raise MutationValidationError(
                        "Native variable dashboard mappings must not include stage-number."
                    )
                continue
            if target_kind != "dimension":
                raise MutationValidationError(
                    "Native dashboard template-tag targets must use variable or dimension."
                )
            if not stage_numbers:
                if not isinstance(target, list):
                    raise MutationValidationError("Dashboard mapping target must be an array.")
                target.append({"stage-number": stage_number})
            elif len(stage_numbers) != 1 or stage_numbers[0] != stage_number:
                raise MutationValidationError(
                    "Dashboard mapping stage-number must match its native query stage."
                )


def _validate_dashboard_parameter_compatibility(
    state: dict[str, Any],
    *,
    require_executable_mappings: bool,
) -> None:
    parameters = state.get("parameters", [])
    dashcards = state.get("dashcards", [])
    ids: set[str] = set()
    families: dict[str, str | None] = {}
    for parameter in parameters:
        if not isinstance(parameter, dict):
            raise MutationValidationError("Dashboard parameters must be objects.")
        parameter_id = parameter.get("id")
        if not isinstance(parameter_id, str) or not parameter_id.strip() or parameter_id in ids:
            raise MutationValidationError(
                "Dashboard parameter ids must be unique non-empty strings."
            )
        ids.add(parameter_id)
        families[parameter_id] = _parameter_family(parameter.get("type"))

    for dashcard in dashcards:
        if not isinstance(dashcard, dict):
            raise MutationValidationError("Dashboard dashcards must be objects.")
        tags = _card_native_template_tags(dashcard.get("card"))
        for mapping in dashcard.get("parameter_mappings", []) or []:
            if not isinstance(mapping, dict):
                raise MutationValidationError("Dashboard parameter mappings must be objects.")
            parameter_id = mapping.get("parameter_id")
            if parameter_id not in ids:
                raise MutationValidationError(
                    "Dashboard parameter mapping references an unknown parameter id."
                )
            tag_name = _template_tag_name(mapping.get("target"))
            if not tag_name:
                continue
            tag_definition = tags.get(tag_name)
            if tag_definition is None:
                raise MutationValidationError(
                    "Dashboard mapping references a missing native template tag."
                )
            tag, stage_number = tag_definition
            tag_family = _parameter_family(tag.get("widget-type")) or _parameter_family(
                tag.get("type")
            )
            parameter_family = families.get(str(parameter_id))
            if tag_family and parameter_family and tag_family != parameter_family:
                raise MutationValidationError(
                    "Dashboard parameter type is incompatible with the mapped native template tag."
                )
            if not require_executable_mappings:
                continue
            card_id = dashcard.get("card_id")
            if type(card_id) is not int or card_id <= 0 or mapping.get("card_id") != card_id:
                raise MutationValidationError(
                    "Native dashboard mappings require the owning positive card_id."
                )
            if stage_number is not None:
                target = mapping.get("target")
                target_kind = _template_tag_target_kind(target)
                stage_numbers = _target_stage_numbers(target)
                if target_kind == "variable":
                    if stage_numbers:
                        raise MutationValidationError(
                            "Native variable dashboard mappings must not include stage-number."
                        )
                elif target_kind == "dimension":
                    if stage_numbers != [stage_number]:
                        raise MutationValidationError(
                            "Native dimension dashboard mappings require the exact "
                            "query stage-number."
                        )
                else:
                    raise MutationValidationError(
                        "Native dashboard template-tag targets must use variable or dimension."
                    )


def validate_state(
    state: dict[str, Any],
    object_type: ObjectType,
    *,
    require_executable_mappings: bool = False,
) -> None:
    if object_type is ObjectType.QUESTION:
        if not isinstance(state.get("name"), str) or not state["name"].strip():
            raise MutationValidationError("Question name must be non-empty.")
        if not isinstance(state.get("display"), str) or not state["display"].strip():
            raise MutationValidationError("Question display must be non-empty.")
        if not isinstance(state.get("dataset_query"), dict):
            raise MutationValidationError("Question dataset_query must be an object.")
        if not isinstance(state.get("visualization_settings"), dict):
            raise MutationValidationError("Question visualization_settings must be an object.")
        for root in ("parameters", "parameter_mappings"):
            if root in state and not isinstance(state[root], list):
                raise MutationValidationError(f"Question {root} must be an array.")
        _positive_or_none(state.get("collection_id"), "Question collection_id")
        if "archived" in state and type(state["archived"]) is not bool:
            raise MutationValidationError("Question archived must be boolean.")
    elif object_type is ObjectType.DASHBOARD:
        if not isinstance(state.get("name"), str) or not state["name"].strip():
            raise MutationValidationError("Dashboard name must be non-empty.")
        for root in ("parameters", "tabs", "dashcards"):
            if root in state and not isinstance(state[root], list):
                raise MutationValidationError(f"Dashboard {root} must be an array.")
        _positive_or_none(state.get("collection_id"), "Dashboard collection_id")
        if "archived" in state and type(state["archived"]) is not bool:
            raise MutationValidationError("Dashboard archived must be boolean.")
        _validate_dashboard_parameter_compatibility(
            state,
            require_executable_mappings=require_executable_mappings,
        )
    elif object_type is ObjectType.COLLECTION:
        if not isinstance(state.get("name"), str) or not state["name"].strip():
            raise MutationValidationError("Collection name must be non-empty.")
        _positive_or_none(state.get("parent_id"), "Collection parent_id")
        if "archived" in state and type(state["archived"]) is not bool:
            raise MutationValidationError("Collection archived must be boolean.")
    elif object_type is ObjectType.FIELD:
        if "display_name" in state and (
            not isinstance(state["display_name"], str) or not state["display_name"].strip()
        ):
            raise MutationValidationError("Field display_name must be non-empty.")
        _positive_or_none(state.get("fk_target_field_id"), "Field fk_target_field_id")
        if "settings" in state and not isinstance(state["settings"], dict):
            raise MutationValidationError("Field settings must be an object.")


def _build_write_payload(
    object_type: ObjectType,
    state: dict[str, Any],
    changed_roots: tuple[str, ...],
) -> dict[str, Any]:
    payload = {root: copy.deepcopy(state.get(root)) for root in changed_roots}
    # Metabase processes dashboard tabs and dashcards as one write contract.
    # Round-trip both arrays when either changes so partial payloads cannot be
    # ignored or interpreted as deleting the omitted sibling collection.
    if object_type is ObjectType.DASHBOARD and set(changed_roots).intersection(
        DASHBOARD_COUPLED_WRITE_ROOTS
    ):
        for root in DASHBOARD_COUPLED_WRITE_ROOTS:
            if not isinstance(state.get(root), list):
                raise MutationValidationError(
                    "Dashboard writes require complete tabs and dashcards snapshots."
                )
            payload[root] = copy.deepcopy(state[root])
        for dashcard in payload["dashcards"]:
            if isinstance(dashcard, dict):
                # GET embeds a server-owned card snapshot. Dashboard writes own
                # only the placement, mappings, and visualization overrides.
                dashcard.pop("card", None)
    return payload


def build_mutation(
    *,
    object_type: ObjectType,
    raw_before: dict[str, Any],
    operations: list[PatchOperation],
    dashboard_cards: dict[int, dict[str, Any]] | None = None,
) -> PlannedMutation:
    if object_type not in MUTABLE_ROOTS:
        raise MutationValidationError("Object type has no update contract.")
    if not 1 <= len(operations) <= 100:
        raise MutationValidationError("Patch must contain between 1 and 100 operations.")
    before = project_state(raw_before, object_type)
    validate_state(_dashboard_validation_state(before, dashboard_cards), object_type)
    after = copy.deepcopy(before)
    changed_roots = tuple(
        dict.fromkeys(_apply_operation(after, object_type, op) for op in operations)
    )
    require_executable_mappings = bool(
        object_type is ObjectType.DASHBOARD
        and set(changed_roots).intersection({"parameters", "dashcards"})
    )
    mapping_change_requested = bool(
        object_type is ObjectType.DASHBOARD
        and (
            "parameters" in changed_roots
            or any(
                operation.path == "/dashcards"
                and (
                    operation.op == "replace_array"
                    or str(operation.item_path).startswith(("/card_id", "/parameter_mappings"))
                )
                for operation in operations
            )
        )
    )
    if mapping_change_requested:
        canonicalize_dashboard_parameter_mappings(after, cards=dashboard_cards)
    validate_state(
        _dashboard_validation_state(after, dashboard_cards),
        object_type,
        require_executable_mappings=require_executable_mappings,
    )
    if canonical_sha256(before) == canonical_sha256(after):
        raise MutationValidationError("Patch produces no state change.")
    payload = _build_write_payload(object_type, after, changed_roots)
    # Metabase's collection PUT schema defaults an omitted archived flag to false.
    # Always bind the current value so a metadata edit cannot implicitly restore it.
    if object_type is ObjectType.COLLECTION:
        payload["archived"] = bool(after.get("archived", False))
    return PlannedMutation(
        object_type=object_type,
        object_id=before["id"],
        before_state=before,
        after_state=after,
        write_payload=payload,
        changed_roots=changed_roots,
        before_sha256=canonical_sha256(before),
        after_sha256=canonical_sha256(after),
    )


def rollback_mutation(source: PlannedMutation, current_raw: dict[str, Any]) -> PlannedMutation:
    if source.before_state is None or source.after_state is None or source.object_id is None:
        raise MutationValidationError("Source mutation has no rollback snapshot.")
    proven_after = source.verified_after_state or source.after_state
    current = project_state(current_raw, source.object_type)
    if canonical_sha256(current) != canonical_sha256(proven_after):
        raise MutationValidationError("Current state no longer matches the proven applied state.")
    for root in source.changed_roots:
        if root not in source.before_state:
            raise MutationValidationError(
                "Rollback cannot restore an absent top-level field safely."
            )
    payload = _build_write_payload(
        source.object_type,
        source.before_state,
        source.changed_roots,
    )
    if source.object_type is ObjectType.COLLECTION:
        payload["archived"] = bool(current.get("archived", False))
    return PlannedMutation(
        object_type=source.object_type,
        object_id=source.object_id,
        before_state=current,
        after_state=copy.deepcopy(source.before_state),
        write_payload=payload,
        changed_roots=source.changed_roots,
        before_sha256=canonical_sha256(current),
        after_sha256=canonical_sha256(source.before_state),
        target={},
    )


def _verification_root_values(
    mutation: PlannedMutation,
    readback_state: dict[str, Any],
    root: str,
) -> tuple[Any, Any]:
    observed = readback_state.get(root)
    expected = mutation.after_state.get(root) if mutation.after_state is not None else None
    before = mutation.before_state.get(root) if mutation.before_state is not None else None
    if (
        mutation.object_type is not ObjectType.DASHBOARD
        or root not in DASHBOARD_COUPLED_WRITE_ROOTS
        or not isinstance(before, list)
        or not isinstance(expected, list)
        or not isinstance(observed, list)
    ):
        return observed, expected
    before_by_id = {
        item["id"]: item
        for item in before
        if isinstance(item, dict) and isinstance(item.get("id"), (int, str))
    }
    ignored_timestamp_ids = {
        item["id"]
        for item in expected
        if isinstance(item, dict)
        and isinstance(item.get("id"), (int, str))
        and item["id"] in before_by_id
        and item.get("updated_at") == before_by_id[item["id"]].get("updated_at")
    }

    def without_unchanged_timestamps(items: list[Any]) -> list[Any]:
        projected = copy.deepcopy(items)
        for item in projected:
            if isinstance(item, dict) and item.get("id") in ignored_timestamp_ids:
                # Metabase refreshes this server-managed child timestamp on dashboard PUTs.
                item.pop("updated_at", None)
        return projected

    return without_unchanged_timestamps(observed), without_unchanged_timestamps(expected)


_DASHBOARD_SERVER_MANAGED_ITEM_KEYS = frozenset({"card", "created_at", "dashboard_id", "entity_id"})


def _dashboard_semantic_item(
    value: Any,
    *,
    tab_ids: dict[int, int] | None = None,
    strip_server_fields: bool = False,
) -> Any:
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            if strip_server_fields and key in _DASHBOARD_SERVER_MANAGED_ITEM_KEYS:
                continue
            if strip_server_fields and key == "id" and type(item) is int and item <= 0:
                continue
            if (
                strip_server_fields
                and key == "dashboard_tab_id"
                and type(item) is int
                and item <= 0
            ):
                if tab_ids and item in tab_ids:
                    projected[key] = tab_ids[item]
                continue
            projected[key] = _dashboard_semantic_item(item, tab_ids=tab_ids)
        return projected
    if isinstance(value, list):
        return [
            _dashboard_semantic_item(
                item,
                tab_ids=tab_ids,
                strip_server_fields=strip_server_fields,
            )
            for item in value
        ]
    return value


def _dashboard_semantic_root(value: Any, *, tab_ids: dict[int, int] | None) -> Any:
    if not isinstance(value, list):
        return _dashboard_semantic_item(value, tab_ids=tab_ids)
    return [
        _dashboard_semantic_item(item, tab_ids=tab_ids, strip_server_fields=True) for item in value
    ]


def _dashboard_tab_id_map(expected: Any, observed: Any) -> dict[int, int] | None:
    if (
        not isinstance(expected, list)
        or not isinstance(observed, list)
        or len(expected) != len(observed)
    ):
        return None
    mapping: dict[int, int] = {}
    for left, right in zip(expected, observed, strict=True):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return None
        left_id = left.get("id")
        right_id = right.get("id")
        if type(left_id) is int and left_id <= 0:
            if type(right_id) is not int or right_id <= 0:
                return None
            mapping[left_id] = right_id
    return mapping


def _protected_root_semantically_matches(
    mutation: PlannedMutation,
    readback_state: dict[str, Any],
    root: str,
) -> bool:
    observed, expected = _verification_root_values(mutation, readback_state, root)
    if root == "dataset_query":
        return dataset_query_semantically_matches(expected, observed)
    if mutation.object_type is not ObjectType.DASHBOARD:
        return _semantic_subset_matches(expected, observed)

    tabs_expected = mutation.after_state.get("tabs") if mutation.after_state else None
    tabs_observed = readback_state.get("tabs")
    tab_ids = _dashboard_tab_id_map(tabs_expected, tabs_observed)
    if root == "tabs" and tab_ids is None:
        return False
    projected_expected = _dashboard_semantic_root(expected, tab_ids=tab_ids)
    projected_observed = _dashboard_semantic_root(observed, tab_ids=None)
    return _semantic_subset_matches(projected_expected, projected_observed)


def verify_mutation(mutation: PlannedMutation, raw_readback: dict[str, Any]) -> bool:
    if mutation.after_state is None or mutation.object_id is None:
        return False
    readback = project_state(raw_readback, mutation.object_type)
    if readback.get("id") != mutation.object_id:
        return False
    roots = set(mutation.changed_roots) | (
        set(PROTECTED_ROOTS.get(mutation.object_type, frozenset())) - set(mutation.changed_roots)
    )
    return all(_protected_root_semantically_matches(mutation, readback, root) for root in roots)


def mutation_summary(mutation: PlannedMutation) -> dict[str, Any]:
    critical = sorted(
        set(mutation.changed_roots) & set(PROTECTED_ROOTS.get(mutation.object_type, ()))
    )
    return {
        "object_type": mutation.object_type.value,
        "object_id": mutation.object_id,
        "changed_roots": list(mutation.changed_roots),
        "critical_roots": critical,
        "before_sha256": mutation.before_sha256,
        "after_sha256": mutation.after_sha256,
        "target": mutation.target,
    }
