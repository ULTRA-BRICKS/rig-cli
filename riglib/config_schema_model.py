"""Declarative descriptors (:class:`Leaf`, :class:`Block`) the schema registry is built from.

A leaf module on purpose: every ``config_schema_blocks_*`` module and ``config_schema_lint``
import these types directly, so nothing has to pass them around to dodge a circular import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Leaf:
    """One scalar/typed key in a block: its JSON type, optional enum, default, and one-line doc.

    ``type`` is a JSON-Schema type name (``boolean``/``string``/``integer``/``array``/``object``),
    or a tuple of type names for nullable/union leaves.
    ``enum`` pins the allowed string values; ``minimum``/``maximum`` are integer bounds (applied
    to the leaf itself, or to each array element when ``type`` is ``array``); ``items_type`` the
    element type of an ``array``; ``items_enum`` pins allowed array entries; ``items_pattern``
    constrains string array entries;
    ``additional_properties_type`` models a string-keyed object map (a tuple of type names for a
    map whose values may be any of several scalar types); ``property_names_pattern`` constrains
    that map's KEYS (``propertyNames``).
    ``not_pattern`` (scalar ``string`` leaves only) rejects any value that MATCHES the given
    regex anywhere — modeled as a ``"not": {"pattern": …}`` sub-schema — for a value whose
    validity is "must not contain X" rather than "must match a shape" (``enum``/``pattern``
    model the latter; a positive pattern can't express a character-blocklist cleanly).
    ``default`` is shown in the schema (editors surface it); ``None`` means "no default
    advertised" (omitted from the emitted node).
    """

    type: str | tuple[str, ...]
    doc: str
    enum: tuple[Any, ...] = ()
    default: Any = None
    minimum: int | None = None
    maximum: int | None = None
    items_type: str | None = None
    items_enum: tuple[str, ...] = ()
    items_pattern: str | None = None
    additional_properties_type: str | tuple[str, ...] | None = None
    property_names_pattern: str | None = None
    not_pattern: str | None = None
    pattern: str | None = None

    def to_node(self) -> dict[str, Any]:
        node_type: Any = list(self.type) if isinstance(self.type, tuple) else self.type
        node: dict[str, Any] = {"type": node_type, "description": self.doc}
        if self.enum:
            node["enum"] = list(self.enum)
        if self.default is not None:
            node["default"] = self.default
        if self.type != "array":
            self._add_bounds(node)
        node.update(self._type_specific_nodes())
        return node

    def _add_bounds(self, node: dict[str, Any]) -> None:
        if self.minimum is not None:
            node["minimum"] = self.minimum
        if self.maximum is not None:
            node["maximum"] = self.maximum

    def _type_specific_nodes(self) -> dict[str, Any]:
        """Keywords only meaningful for one JSON type; ordering matches the published file."""
        if self.type == "array":
            return self._array_nodes()
        if self.type == "object":
            return self._object_nodes()
        if self.type == "string":
            return self._string_nodes()
        return {}

    def _array_nodes(self) -> dict[str, Any]:
        if not self.items_type:
            return {}
        items: dict[str, Any] = {"type": self.items_type}
        self._add_bounds(items)
        if self.items_enum:
            items["enum"] = list(self.items_enum)
        if self.items_pattern:
            items["pattern"] = self.items_pattern
        return {"items": items}

    def _object_nodes(self) -> dict[str, Any]:
        nodes: dict[str, Any] = {}
        ap_type = self.additional_properties_type
        if ap_type:
            nodes["additionalProperties"] = {"type": list(ap_type) if isinstance(ap_type, tuple) else ap_type}
        if self.property_names_pattern:
            nodes["propertyNames"] = {"pattern": self.property_names_pattern}
        return nodes

    def _string_nodes(self) -> dict[str, Any]:
        nodes: dict[str, Any] = {}
        if self.not_pattern:
            nodes["not"] = {"pattern": self.not_pattern}
        if self.pattern:
            nodes["pattern"] = self.pattern
        return nodes


@dataclass(frozen=True)
class Block:
    """One config block (a top-level key, or a nested object inside one).

    ``leaves`` are the fixed, named keys. ``nested`` are sub-blocks (each itself a :class:`Block`).
    ``open_map`` names an arbitrary-keyed child map this block permits (``items`` for ci/mcp/
    agent_hooks/skills.by_type, ``fragments`` for git_hooks.dispatcher, ``jobs`` for dev.e2e), so
    the block stays open under that ONE key while every other key is still rejected.
    ``open_map_item`` optionally pins the SHAPE of each map VALUE: when set, the published schema
    models every item with that block's ``required``/``properties``/``enum`` (so an editor flags a
    missing ``content`` or a bad ``role``), matching the Python validator. When ``None`` (ci/mcp/
    agent_hooks — whose item shapes are catalog-defined and open by design) each item stays a
    permissive ``{"type": "object"}``. ``closed`` (default True) emits ``additionalProperties: false``
    so an unknown FIXED key fails; a block with an ``open_map`` carries an explicit
    ``additionalProperties`` allowing only the map. ``additional_properties`` is for a top-level
    dictionary whose own keys are the user-defined entries (``scripts``). ``doc`` is the block's
    one-line description.
    """

    doc: str
    leaves: dict[str, Leaf] = field(default_factory=dict)
    nested: dict[str, Block] = field(default_factory=dict)
    open_map: str | None = None
    open_map_doc: str = ""
    open_map_item: Block | None = None
    open_map_item_required: tuple[str, ...] = ()
    additional_properties: Any | None = None
    schema_extra: dict[str, Any] = field(default_factory=dict)
    closed: bool = True
    # nested blocks only (review finding, tg#11652): when the Python validator treats an
    # explicit `null` for this nested section the same as absent (an `is not None` guard, or the
    # null-specific `{} if x is None else x` normalization — deliberately NOT a plain `or {}`,
    # which would also swallow other falsy-but-invalid values like `False`; see
    # `task.github`/`task.linear`), the schema must accept it too — otherwise editors and
    # schema-based CI reject a config the runtime validator happily accepts. Emits
    # `"type": ["object", "null"]` instead of the plain `"object"`.
    nullable: bool = False

    def child_keys(self) -> set[str]:
        keys = set(self.leaves) | set(self.nested)
        if self.open_map:
            keys.add(self.open_map)
        return keys

    def to_node(self) -> dict[str, Any]:
        props: dict[str, Any] = {name: leaf.to_node() for name, leaf in self.leaves.items()}
        for name, blk in self.nested.items():
            props[name] = blk.to_node()
        node_type: Any = ["object", "null"] if self.nullable else "object"
        node: dict[str, Any] = {"type": node_type, "description": self.doc, "properties": props}
        if self.open_map:
            props[self.open_map] = self._open_map_node()
        if self.additional_properties is not None:
            node["additionalProperties"] = self.additional_properties
        else:
            node["additionalProperties"] = False if self.closed else True
        node.update(self.schema_extra)
        return node

    def _open_map_node(self) -> dict[str, Any]:
        """The open child map: an object whose keys are arbitrary names.

        By default each item's inner shape is not modeled (catalog-defined, open by design) → a
        permissive object-of-objects. When ``open_map_item`` IS set, the item shape is modeled so the
        published schema enforces required keys / enums exactly like the Python validator.
        """
        additional: Any = {"type": "object"}
        if self.open_map_item is not None:
            additional = self.open_map_item.to_node()
            if self.open_map_item_required:
                additional["required"] = list(self.open_map_item_required)
        return {
            "type": "object",
            "description": self.open_map_doc or "per-item overrides keyed by item name",
            "additionalProperties": additional,
        }
