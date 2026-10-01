import re

from frappe.tests import UnitTestCase

from atlas.atlas.core.tags import MAXIMUM_TAG_KEY_LENGTH, MAXIMUM_TAG_VALUE_LENGTH, MAXIMUM_TAGS
from atlas.vm.core.placement.affinity import (
	MAXIMUM_AFFINITY_GROUP_DEPTH,
	MAXIMUM_AFFINITY_RULES,
	AffinityGroup,
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

	def test_invalid_rules_are_rejected(self) -> None:
		cases = [
			("Affinity rules must be a list.", NO_CARGO_SERVER),
			("An affinity rule or group must be an object.", ["has_not"]),
			("exactly one key", [{"any_of": [STORAGE_HOST], "all_of": [STORAGE_HOST]}]),
			("exactly one key", [{"any_of": [STORAGE_HOST], "resource": "metal_server"}]),
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
