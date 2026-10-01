"""Affinity rules that limit the Metal Servers that can hold a virtual machine."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, assert_never, cast, get_args

import frappe

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.atlas.core.tags import (
	MAXIMUM_TAG_KEY_LENGTH,
	MAXIMUM_TAG_VALUE_LENGTH,
	MAXIMUM_TAGS,
	read_tags_for,
)

AffinityResource = Literal["metal_server", "virtual_machine"]
AffinityOperator = Literal["has", "has_not"]
AffinityGroupKind = Literal["any_of", "all_of"]
AffinityMatching = Literal["Enforced", "Preferred"]

AFFINITY_RULE_FIELDS = ("resource", "operator", "tags")
MAXIMUM_AFFINITY_RULES = 16
MAXIMUM_AFFINITY_GROUP_DEPTH = 5
# A migration in these states reserves its destination host, as in the capacity query.
RESERVED_MIGRATION_STATUSES = ("preparing", "copying", "cutting_over", "starting", "finalizing", "canceling")


class AffinityUnsatisfied(AtlasUserError):
	"""No Metal Server that meets the affinity rules can hold the requested VM."""

	code = "affinity_unsatisfied"
	http_status_code = 503


def load_affinity_matching() -> AffinityMatching:
	"""Read how strictly placement applies affinity rules. An unsaved setting uses its default."""
	value = frappe.get_cached_value("Atlas Settings", "Atlas Settings", "affinity_matching")
	if not value:
		value = frappe.get_meta("Atlas Settings").get_field("affinity_matching").default
	if value not in get_args(AffinityMatching):
		raise ValueError(f"Unknown affinity matching setting: {value}.")

	return cast(AffinityMatching, value)


@dataclass(frozen=True, slots=True)
class AffinityHost:
	"""The tags that affinity rules read on one candidate Metal Server."""

	name: str
	tags: dict[str, str]
	virtual_machine_tags: tuple[dict[str, str], ...]

	@classmethod
	def load_many(
		cls, host_names: Sequence[str], tenant_id: int, excluded_virtual_machine: str | None = None
	) -> list[AffinityHost]:
		"""Load each host's tags and the tags of the tenant VMs that it holds.

		A VM counts on its assigned host, also as a draft or while it terminates, and on the
		destination of its active migration. `excluded_virtual_machine` never counts.
		"""
		host_tags = read_tags_for("Metal Server", list(host_names))
		placements = cls._find_virtual_machine_hosts(host_names, tenant_id, excluded_virtual_machine)
		virtual_machine_tags = read_tags_for("Virtual Machine", sorted({name for name, _ in placements}))

		tags_by_host: dict[str, list[dict[str, str]]] = {name: [] for name in host_names}
		for virtual_machine, host_name in placements:
			tags_by_host[host_name].append(virtual_machine_tags[virtual_machine])

		return [cls(name, host_tags[name], tuple(tags_by_host[name])) for name in host_names]

	@staticmethod
	def _find_virtual_machine_hosts(
		host_names: Sequence[str], tenant_id: int, excluded_virtual_machine: str | None
	) -> list[tuple[str, str]]:
		"""Return a (VM, host) pair for each assigned host and each active migration destination."""
		if not host_names:
			return []

		virtual_machine = frappe.qb.DocType("Virtual Machine")
		migration = frappe.qb.DocType("Virtual Machine Migration")
		assigned = (
			frappe.qb.from_(virtual_machine)
			.select(virtual_machine.name, virtual_machine.server)
			.where(virtual_machine.server.isin(list(host_names)) & (virtual_machine.tenant_id == tenant_id))
		)
		incoming = (
			frappe.qb.from_(migration)
			.join(virtual_machine)
			.on(virtual_machine.name == migration.virtual_machine)
			.select(virtual_machine.name, migration.destination_metal_server)
			.where(
				migration.destination_metal_server.isin(list(host_names))
				& migration.status.isin(RESERVED_MIGRATION_STATUSES)
				& (virtual_machine.tenant_id == tenant_id)
			)
		)
		if excluded_virtual_machine:
			assigned = assigned.where(virtual_machine.name != excluded_virtual_machine)
			incoming = incoming.where(virtual_machine.name != excluded_virtual_machine)

		return [*assigned.run(), *incoming.run()]


@dataclass(frozen=True, slots=True)
class AffinityRule:
	"""Require or reject tags on the candidate Metal Server, or on one VM that runs on it."""

	resource: AffinityResource
	operator: AffinityOperator
	tags: dict[str, str]

	def __post_init__(self) -> None:
		"""Reject a rule whose fields do not match their types."""
		if self.resource not in get_args(AffinityResource):
			raise ValueError(
				f"Affinity rule resource must be one of: {', '.join(get_args(AffinityResource))}."
			)
		if self.operator not in get_args(AffinityOperator):
			raise ValueError(
				f"Affinity rule operator must be one of: {', '.join(get_args(AffinityOperator))}."
			)
		if not isinstance(self.tags, dict) or any(
			not isinstance(key, str) or not isinstance(value, str) for key, value in self.tags.items()
		):
			raise TypeError("Affinity rule tags must be a dict of strings to strings.")
		if not self.tags:
			raise ValueError("Affinity rule tags must be an object with at least one key.")

	@classmethod
	def from_value(cls, value: dict[str, object]) -> AffinityRule:
		"""Parse one rule. Every tag pair must be on the same resource."""
		unknown = set(value) - set(AFFINITY_RULE_FIELDS)
		if unknown:
			raise ValueError(f"Unknown affinity rule field: {sorted(unknown)[0]}.")
		missing = [name for name in AFFINITY_RULE_FIELDS if name not in value]
		if missing:
			raise ValueError(f"An affinity rule needs {missing[0]}.")

		# The constructor checks the resource and the operator.
		return cls(
			resource=cast(AffinityResource, value["resource"]),
			operator=cast(AffinityOperator, value["operator"]),
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

	def is_satisfied_by(self, host: AffinityHost) -> bool:
		"""Return whether the host meets this rule. Every pair must be on the same resource."""
		if self.resource == "metal_server":
			has_tags = self._is_found_in(host.tags)
		elif self.resource == "virtual_machine":
			has_tags = False
			for virtual_machine_tags in host.virtual_machine_tags:
				if self._is_found_in(virtual_machine_tags):
					has_tags = True
					break
		else:
			assert_never(self.resource)

		if self.operator == "has":
			return has_tags
		elif self.operator == "has_not":
			return not has_tags
		else:
			assert_never(self.operator)

	def _is_found_in(self, tags: dict[str, str]) -> bool:
		"""Return whether `tags` holds every key of this rule, with the same value."""
		for key in self.tags:
			if not (key in tags and self.tags[key] == tags[key]):
				return False

		return True

	def as_dict(self) -> dict[str, object]:
		"""Return the JSON-compatible rule."""
		return {"resource": self.resource, "operator": self.operator, "tags": dict(self.tags)}


@dataclass(frozen=True, slots=True)
class AffinityGroup:
	"""Combine nodes. `any_of` holds when one node holds, and `all_of` when every node holds."""

	kind: AffinityGroupKind
	nodes: tuple[AffinityNode, ...]

	def __post_init__(self) -> None:
		"""Reject a group whose kind or nodes do not match their types."""
		if self.kind not in get_args(AffinityGroupKind):
			raise ValueError(f"Affinity group kind must be one of: {', '.join(get_args(AffinityGroupKind))}.")
		if not isinstance(self.nodes, tuple) or any(
			not isinstance(node, AffinityNode) for node in self.nodes
		):
			raise TypeError("An affinity group holds a tuple of rules and groups.")
		if not self.nodes:
			raise ValueError(f"Affinity group {self.kind} must be a list with at least one rule or group.")

	@classmethod
	def from_value(cls, kind: AffinityGroupKind, value: object, depth: int) -> AffinityGroup:
		"""Parse one group. `depth` is 1 for a top-level group."""
		if depth > MAXIMUM_AFFINITY_GROUP_DEPTH:
			raise ValueError(f"Affinity groups nest at most {MAXIMUM_AFFINITY_GROUP_DEPTH} deep.")
		if not isinstance(value, list):
			raise ValueError(f"Affinity group {kind} must be a list with at least one rule or group.")

		# The constructor checks the kind and rejects an empty group.
		return cls(kind=kind, nodes=tuple(_parse_node(node, depth) for node in value))

	def is_satisfied_by(self, host: AffinityHost) -> bool:
		"""Return whether the host meets one node (`any_of`) or every node (`all_of`)."""
		results = (node.is_satisfied_by(host) for node in self.nodes)
		if self.kind == "any_of":
			return any(results)
		elif self.kind == "all_of":
			return all(results)
		else:
			assert_never(self.kind)

	def as_dict(self) -> dict[str, object]:
		"""Return the JSON-compatible group."""
		return {self.kind: [node.as_dict() for node in self.nodes]}


AffinityNode = AffinityRule | AffinityGroup


@dataclass(frozen=True, slots=True)
class AffinityRules:
	"""The affinity rules of one virtual machine. Every top-level node must hold."""

	nodes: tuple[AffinityNode, ...] = ()

	def __post_init__(self) -> None:
		"""Reject top-level nodes that are not rules or groups."""
		if not isinstance(self.nodes, tuple) or any(
			not isinstance(node, AffinityNode) for node in self.nodes
		):
			raise TypeError("Affinity rules hold a tuple of rules and groups.")

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

	@classmethod
	def from_json(cls, value: str | None) -> AffinityRules:
		"""Parse the rules that a Virtual Machine stores. An empty value means no rules."""
		return cls.from_value(json.loads(value) if value else None)

	def filter_hosts(
		self, host_names: Sequence[str], tenant_id: int, excluded_virtual_machine: str | None = None
	) -> list[str]:
		"""Return the hosts that meet every rule, in the given order.

		VM rules read only the VMs of `tenant_id`. `excluded_virtual_machine` is the VM that
		placement moves, so it never matches its own rules.
		"""
		if not self.nodes:
			return list(host_names)

		hosts = AffinityHost.load_many(host_names, tenant_id, excluded_virtual_machine)
		return [host.name for host in hosts if self.is_satisfied_by(host)]

	def is_satisfied_by(self, host: AffinityHost) -> bool:
		"""Return whether the host meets every top-level node."""
		return all(node.is_satisfied_by(host) for node in self.nodes)

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
