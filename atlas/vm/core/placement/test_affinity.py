import re
from uuid import uuid7

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from atlas.atlas.core.tags import MAXIMUM_TAG_KEY_LENGTH, MAXIMUM_TAG_VALUE_LENGTH, MAXIMUM_TAGS
from atlas.vm.core.placement.affinity import (
	MAXIMUM_AFFINITY_GROUP_DEPTH,
	MAXIMUM_AFFINITY_RULES,
	AffinityGroup,
	AffinityHost,
	AffinityRule,
	AffinityRules,
)

STORAGE_HOST = {"resource": "metal_server", "operator": "has", "tags": {"type": "storage-optimised"}}
RACK_A_HOST = {"resource": "metal_server", "operator": "has", "tags": {"rack": "a"}}
NO_CARGO_SERVER = {"resource": "virtual_machine", "operator": "has_not", "tags": {"role": "cargo-server"}}


def nest(node: dict, depth: int) -> dict:
	"""Wrap one node in `depth` groups that alternate between any_of and all_of."""
	for level in range(depth):
		node = {("any_of", "all_of")[level % 2]: [node]}
	return node


def rule(resource: str, operator: str, **tags: str) -> dict:
	return {"resource": resource, "operator": operator, "tags": tags}


def insert(doctype: str, **values: object) -> str:
	"""Insert one row and its child rows without document hooks, and return its name."""
	document = frappe.get_doc({"doctype": doctype, **values})
	document.db_insert()
	for row in document.get_all_children():
		row.db_insert()
	return document.name


class TestAffinityRules(UnitTestCase):
	def test_nested_rules_keep_their_shape(self) -> None:
		value = [
			{"any_of": [{"all_of": [STORAGE_HOST, RACK_A_HOST]}, STORAGE_HOST]},
			NO_CARGO_SERVER,
		]

		rules = AffinityRules.from_value(value)

		storage = AffinityRule("metal_server", "has", {"type": "storage-optimised"})
		rack_a = AffinityRule("metal_server", "has", {"rack": "a"})
		self.assertEqual(
			rules,
			AffinityRules(
				(
					AffinityGroup("any_of", (AffinityGroup("all_of", (storage, rack_a)), storage)),
					AffinityRule("virtual_machine", "has_not", {"role": "cargo-server"}),
				)
			),
		)
		self.assertEqual(rules.as_list(), value)

	def test_absent_and_empty_rules_mean_no_rules(self) -> None:
		self.assertEqual(AffinityRules.from_value(None), AffinityRules())
		self.assertEqual(AffinityRules.from_value([]), AffinityRules())

	def test_tag_pairs_are_trimmed(self) -> None:
		rules = AffinityRules.from_value([{**RACK_A_HOST, "tags": {" rack ": " a "}}])

		self.assertEqual(rules.as_list(), [RACK_A_HOST])

	def test_rules_at_the_limits_are_accepted(self) -> None:
		AffinityRules.from_value([NO_CARGO_SERVER] * MAXIMUM_AFFINITY_RULES)
		AffinityRules.from_value([nest(NO_CARGO_SERVER, MAXIMUM_AFFINITY_GROUP_DEPTH)])

	def test_construction_checks_every_field_type(self) -> None:
		rule = AffinityRule("metal_server", "has", {"rack": "a"})
		cases = [
			("unknown resource", ValueError, lambda: AffinityRule("rack", "has", {"rack": "a"})),
			("unknown operator", ValueError, lambda: AffinityRule("metal_server", "in", {"rack": "a"})),
			("empty tags", ValueError, lambda: AffinityRule("metal_server", "has", {})),
			(
				"tag value that is not a string",
				TypeError,
				lambda: AffinityRule("metal_server", "has", {"rack": 1}),
			),
			("unknown group kind", ValueError, lambda: AffinityGroup("none_of", (rule,))),
			("empty group", ValueError, lambda: AffinityGroup("any_of", ())),
			("group nodes in a list", TypeError, lambda: AffinityGroup("any_of", [rule])),
			("group node that is not a rule", TypeError, lambda: AffinityGroup("all_of", ("rule",))),
			("top-level node that is a dict", TypeError, lambda: AffinityRules((STORAGE_HOST,))),
		]
		for label, error, build in cases:
			with self.subTest(label), self.assertRaises(error):
				build()

	def test_invalid_rules_are_rejected(self) -> None:
		cases = [
			("Affinity rules must be a list.", NO_CARGO_SERVER),
			("An affinity rule or group must be an object.", ["has_not"]),
			("exactly one key", [{"any_of": [STORAGE_HOST], "all_of": [STORAGE_HOST]}]),
			("exactly one key", [{"any_of": [STORAGE_HOST], "resource": "metal_server"}]),
			# An unknown group key is not a group, so the rule parser rejects it.
			("Unknown affinity rule field: none_of.", [{"none_of": [STORAGE_HOST]}]),
			("at least one rule or group", [{"all_of": []}]),
			("Unknown affinity rule field: within.", [{**NO_CARGO_SERVER, "within": "rack"}]),
			("An affinity rule needs operator.", [{"resource": "metal_server", "tags": {"rack": "a"}}]),
			("resource must be one of", [{**STORAGE_HOST, "resource": "metal-server"}]),
			("operator must be one of", [{**STORAGE_HOST, "operator": "in"}]),
			("at least one key", [{**STORAGE_HOST, "tags": {}}]),
			("must be strings", [{**STORAGE_HOST, "tags": {"rack": 1}}]),
			("needs a key", [{**STORAGE_HOST, "tags": {" ": "a"}}]),
			(
				"Affinity rule tag key rack is repeated.",
				[{**STORAGE_HOST, "tags": {"rack": "a", " rack": "b"}}],
			),
			("key takes at most", [{**STORAGE_HOST, "tags": {"k" * (MAXIMUM_TAG_KEY_LENGTH + 1): "a"}}]),
			(
				"value of affinity rule tag key rack",
				[{**STORAGE_HOST, "tags": {"rack": "v" * (MAXIMUM_TAG_VALUE_LENGTH + 1)}}],
			),
			(
				f"at most {MAXIMUM_TAGS} tags",
				[{**STORAGE_HOST, "tags": {f"key-{index}": "a" for index in range(MAXIMUM_TAGS + 1)}}],
			),
			("nest at most", [nest(NO_CARGO_SERVER, MAXIMUM_AFFINITY_GROUP_DEPTH + 1)]),
			("affinity rules", [NO_CARGO_SERVER] * (MAXIMUM_AFFINITY_RULES + 1)),
			# Rules inside groups count toward the same limit.
			("affinity rules", [{"any_of": [NO_CARGO_SERVER] * (MAXIMUM_AFFINITY_RULES + 1)}]),
		]
		for message, value in cases:
			with self.subTest(message=message), self.assertRaisesRegex(ValueError, re.escape(message)):
				AffinityRules.from_value(value)


class TestAffinityEvaluation(UnitTestCase):
	def test_rules_read_the_host_and_its_virtual_machines(self) -> None:
		storage_in_rack_a = AffinityHost("a", {"type": "storage-optimised", "rack": "a"}, ())
		memory_host = AffinityHost("b", {"type": "memory-optimised"}, ())
		split_pairs = AffinityHost("c", {}, ({"role": "db"}, {"env": "prod"}))
		one_vm_with_both = AffinityHost("d", {}, ({"role": "db", "env": "prod"},))
		cases = [
			("a host rule reads the host tags", [STORAGE_HOST], storage_in_rack_a, True),
			("a host rule needs the same value", [STORAGE_HOST], memory_host, False),
			(
				"has_not rejects a host with every pair",
				[rule("metal_server", "has_not", type="storage-optimised")],
				storage_in_rack_a,
				False,
			),
			(
				"has_not accepts a host without a pair",
				[rule("metal_server", "has_not", type="storage-optimised", rack="b")],
				storage_in_rack_a,
				True,
			),
			(
				"a VM rule needs every pair on one VM",
				[rule("virtual_machine", "has", role="db", env="prod")],
				split_pairs,
				False,
			),
			(
				"a VM rule accepts one VM with every pair",
				[rule("virtual_machine", "has", role="db", env="prod")],
				one_vm_with_both,
				True,
			),
			(
				"all_of accepts pairs on different VMs",
				[
					{
						"all_of": [
							rule("virtual_machine", "has", role="db"),
							rule("virtual_machine", "has", env="prod"),
						]
					}
				],
				split_pairs,
				True,
			),
			("has_not accepts a host without VMs", [NO_CARGO_SERVER], memory_host, True),
			("any_of needs one node", [{"any_of": [STORAGE_HOST, RACK_A_HOST]}], memory_host, False),
			(
				"any_of accepts one matching node",
				[{"any_of": [rule("metal_server", "has", type="memory-optimised"), RACK_A_HOST]}],
				memory_host,
				True,
			),
			(
				"every top-level node must hold",
				[STORAGE_HOST, rule("metal_server", "has", rack="b")],
				storage_in_rack_a,
				False,
			),
			("no rules accept every host", [], memory_host, True),
		]
		for label, value, host, expected in cases:
			with self.subTest(label):
				self.assertEqual(AffinityRules.from_value(value).is_satisfied_by(host), expected)


class TestAffinityHostFilter(IntegrationTestCase):
	def setUp(self) -> None:
		super().setUp()
		# Metal Server and Virtual Machine Migration names are UUID columns, so they reject other values.
		self.storage_host = insert(
			"Metal Server", name=str(uuid7()), tags=[{"key": "type", "value": "storage-optimised"}]
		)
		self.memory_host = insert(
			"Metal Server", name=str(uuid7()), tags=[{"key": "type", "value": "memory-optimised"}]
		)
		self.plain_host = insert("Metal Server", name=str(uuid7()))
		self.hosts = [self.storage_host, self.memory_host, self.plain_host]

		self.cargo_server = self.virtual_machine(0, self.storage_host, role="cargo-server")
		self.virtual_machine(7, self.memory_host, role="cargo-server")
		moving_database = self.virtual_machine(0, self.plain_host, role="db")
		self.migration(moving_database, self.memory_host, "copying")
		moved_archive = self.virtual_machine(0, self.plain_host, role="archive")
		self.migration(moved_archive, self.storage_host, "completed")

	def virtual_machine(self, tenant_id: int, host: str, **tags: str) -> str:
		return insert(
			"Virtual Machine",
			name=f"test-vm-{frappe.generate_hash(length=8)}",
			tenant_id=tenant_id,
			server=host,
			tags=[{"key": key, "value": value} for key, value in tags.items()],
		)

	def migration(self, virtual_machine: str, destination: str, status: str) -> None:
		insert(
			"Virtual Machine Migration",
			name=str(uuid7()),
			virtual_machine=virtual_machine,
			destination_metal_server=destination,
			status=status,
		)

	def filter_hosts(self, value: list, excluded_virtual_machine: str | None = None) -> list[str]:
		return AffinityRules.from_value(value).filter_hosts(self.hosts, 0, excluded_virtual_machine)

	def test_host_rules_keep_the_given_order(self) -> None:
		rules = AffinityRules.from_value(
			[{"any_of": [STORAGE_HOST, rule("metal_server", "has", type="memory-optimised")]}]
		)

		self.assertEqual(
			rules.filter_hosts([self.memory_host, self.plain_host, self.storage_host], 0),
			[self.memory_host, self.storage_host],
		)

	def test_virtual_machine_rules_read_only_the_tenant_virtual_machines(self) -> None:
		self.assertEqual(self.filter_hosts([NO_CARGO_SERVER]), [self.memory_host, self.plain_host])

	def test_the_excluded_virtual_machine_never_matches(self) -> None:
		self.assertEqual(self.filter_hosts([NO_CARGO_SERVER], self.cargo_server), self.hosts)

	def test_an_active_migration_counts_on_its_destination(self) -> None:
		self.assertEqual(
			self.filter_hosts([rule("virtual_machine", "has", role="db")]),
			[self.memory_host, self.plain_host],
		)
		self.assertEqual(
			self.filter_hosts([rule("virtual_machine", "has", role="archive")]), [self.plain_host]
		)

	def test_no_rules_keep_every_host_without_a_query(self) -> None:
		with self.assertQueryCount(0):
			self.assertEqual(AffinityRules().filter_hosts(self.hosts, 0), self.hosts)
