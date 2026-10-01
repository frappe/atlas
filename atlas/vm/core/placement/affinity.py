"""Affinity rules that limit the Metal Servers that can hold a virtual machine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast, get_args

from atlas.atlas.core.tags import MAXIMUM_TAG_KEY_LENGTH, MAXIMUM_TAG_VALUE_LENGTH, MAXIMUM_TAGS

AffinityResource = Literal["metal_server", "virtual_machine"]
AffinityOperator = Literal["has", "has_not"]
AffinityGroupKind = Literal["any_of", "all_of"]

AFFINITY_RULE_FIELDS = ("resource", "operator", "tags")
MAXIMUM_AFFINITY_RULES = 16
MAXIMUM_AFFINITY_GROUP_DEPTH = 3


@dataclass(frozen=True, slots=True)
class AffinityRule:
	"""Require or reject tags on the candidate Metal Server, or on one VM that runs on it."""

	resource: AffinityResource
	operator: AffinityOperator
	tags: dict[str, str]

	@classmethod
	def from_value(cls, value: dict[str, object]) -> AffinityRule:
		"""Parse one rule. Every tag pair must be on the same resource."""
		unknown = set(value) - set(AFFINITY_RULE_FIELDS)
		if unknown:
			raise ValueError(f"Unknown affinity rule field: {sorted(unknown)[0]}.")
		missing = [name for name in AFFINITY_RULE_FIELDS if name not in value]
		if missing:
			raise ValueError(f"An affinity rule needs {missing[0]}.")

		resource = value["resource"]
		if resource not in get_args(AffinityResource):
			raise ValueError(
				f"Affinity rule resource must be one of: {', '.join(get_args(AffinityResource))}."
			)
		operator = value["operator"]
		if operator not in get_args(AffinityOperator):
			raise ValueError(
				f"Affinity rule operator must be one of: {', '.join(get_args(AffinityOperator))}."
			)

		return cls(
			resource=cast(AffinityResource, resource),
			operator=cast(AffinityOperator, operator),
			tags=cls._parse_tags(value["tags"]),
		)

	@classmethod
	def _parse_tags(cls, value: object) -> dict[str, str]:
		"""Trim the tag pairs and apply the Atlas Tag limits."""
		if not isinstance(value, dict) or not value:
			raise ValueError("Affinity rule tags must be an object with at least one key.")
		if len(value) > MAXIMUM_TAGS:
			raise ValueError(f"An affinity rule takes at most {MAXIMUM_TAGS} tags.")

		tags: dict[str, str] = {}
		for raw_key, raw_value in value.items():
			key, tag_value = cls._parse_tag(raw_key, raw_value)
			if key in tags:
				raise ValueError(f"Affinity rule tag key {key} is repeated.")
			tags[key] = tag_value

		return tags

	@staticmethod
	def _parse_tag(key: object, value: object) -> tuple[str, str]:
		if not isinstance(key, str) or not isinstance(value, str):
			raise ValueError("Affinity rule tag keys and values must be strings.")

		key, value = key.strip(), value.strip()
		if not key:
			raise ValueError("An affinity rule tag needs a key.")
		if len(key) > MAXIMUM_TAG_KEY_LENGTH:
			raise ValueError(f"An affinity rule tag key takes at most {MAXIMUM_TAG_KEY_LENGTH} characters.")
		if len(value) > MAXIMUM_TAG_VALUE_LENGTH:
			raise ValueError(
				f"The value of affinity rule tag key {key} takes at most {MAXIMUM_TAG_VALUE_LENGTH} characters."
			)

		return key, value

	def as_dict(self) -> dict[str, object]:
		"""Return the JSON-compatible rule."""
		return {"resource": self.resource, "operator": self.operator, "tags": dict(self.tags)}


@dataclass(frozen=True, slots=True)
class AffinityGroup:
	"""Combine nodes. `any_of` holds when one node holds, and `all_of` when every node holds."""

	kind: AffinityGroupKind
	nodes: tuple[AffinityNode, ...]

	@classmethod
	def from_value(cls, kind: AffinityGroupKind, value: object, depth: int) -> AffinityGroup:
		"""Parse one group. `depth` is 1 for a top-level group."""
		if depth > MAXIMUM_AFFINITY_GROUP_DEPTH:
			raise ValueError(f"Affinity groups nest at most {MAXIMUM_AFFINITY_GROUP_DEPTH} deep.")
		if not isinstance(value, list) or not value:
			raise ValueError(f"Affinity group {kind} must be a list with at least one rule or group.")

		return cls(kind=kind, nodes=tuple(_parse_node(node, depth) for node in value))

	def as_dict(self) -> dict[str, object]:
		"""Return the JSON-compatible group."""
		return {self.kind: [node.as_dict() for node in self.nodes]}


AffinityNode = AffinityRule | AffinityGroup


@dataclass(frozen=True, slots=True)
class AffinityRules:
	"""The affinity rules of one virtual machine. Every top-level node must hold."""

	nodes: tuple[AffinityNode, ...] = ()

	@classmethod
	def from_value(cls, value: object | None) -> AffinityRules:
		"""Parse and validate a JSON rule list. None means no rules."""
		if value is None:
			return cls()
		if not isinstance(value, list):
			raise ValueError("Affinity rules must be a list.")

		nodes = tuple(_parse_node(node, depth=0) for node in value)
		if _count_rules(nodes) > MAXIMUM_AFFINITY_RULES:
			raise ValueError(f"A virtual machine takes at most {MAXIMUM_AFFINITY_RULES} affinity rules.")

		return cls(nodes=nodes)

	def as_list(self) -> list[dict[str, object]]:
		"""Return the JSON-compatible rule list."""
		return [node.as_dict() for node in self.nodes]


def _parse_node(value: object, depth: int) -> AffinityNode:
	"""Parse one rule or group. `depth` counts the groups that enclose the node."""
	if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
		raise ValueError("An affinity rule or group must be an object.")

	kinds = [kind for kind in get_args(AffinityGroupKind) if kind in value]
	if not kinds:
		return AffinityRule.from_value(value)
	if len(value) != 1:
		raise ValueError("An affinity group takes exactly one key: any_of or all_of.")

	return AffinityGroup.from_value(kinds[0], value[kinds[0]], depth + 1)


def _count_rules(nodes: tuple[AffinityNode, ...]) -> int:
	return sum(1 if isinstance(node, AffinityRule) else _count_rules(node.nodes) for node in nodes)
