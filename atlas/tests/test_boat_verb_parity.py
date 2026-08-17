"""Structural parity the Boat-verb Python deletion relies on (spec/33, item 9).

Boat now implements every host/VM verb the ten-plus-six `scripts/*.py` used to,
so those `.py` are being deleted. The behaviour parity — that `boat snapshot-vm`
does on a host what `snapshot-vm.py` did — is proven by Boat's own golden and
behaviour tests under `/home/qwerty/boat/internal/**` (they were generated FROM
these scripts) plus the live proofs already run on real hosts. Those Go tests are
the behaviour oracle and do not depend on the `.py` existing.

What CANNOT lean on the deleted files is the Atlas-side seam that routes a verb to
Boat. This module pins exactly that, so it survives the deletion it guards:

  - every verb in `scripts_catalog.BOAT_VERBS` renders as `boat <verb> --flags`
    with the SAME `--kebab-case` flags an `atlas <verb>` rendering would carry —
    the runner's first-word swap is the only change, so the boat command is
    flag-compatible with the old one (`_remote_command` is the whole seam);
  - a `BOAT_ONLY_VERBS` verb (no `.py` on disk) still answers every question the
    catalog is asked without a file: `kind()` is "python" (the flag-taking command
    shape), `file_for()` raises, nothing ships durably for it;
  - a `_PORTED_VERBS` verb still has its on-disk oracle while it is in that set (a
    verb LEAVES the set the moment its file is deleted, so this stays true at every
    step of the deletion);
  - `runs_on_boat()` is True for exactly `BOAT_VERBS` and nothing else;
  - for every verb with a Fake result builder, the typed result shape the
    controller parses back is still produced — that synthesis lives in
    `providers/fake_tasks`, independent of any `.py`, and must remain so.

`EXPECTED_BOAT_VERBS` is the anchor: the exact set of verbs Boat serves through the
first-word swap. It is pinned as a literal so that moving a verb from
`_PORTED_VERBS` into `BOAT_ONLY_VERBS` (which each deletion gate does) cannot
silently change WHICH verbs route to Boat — only where the catalog records them.
"""

import json
import unittest

from atlas.atlas.core import scripts_catalog
from atlas.atlas.core._ssh import runner
from atlas.atlas.core.providers import fake_tasks
from atlas.atlas.core.task_results import parse_result, result_line

# The verbs Boat serves through the `boat <verb>` first-word swap: the ten
# original ports, provision + the five later ports, and the one BOAT_ONLY rename
# (`bootstrap`). Membership here is preserved across the deletion — before a
# verb's file is deleted it lives in _PORTED_VERBS, after in BOAT_ONLY_VERBS, but
# it is a BOAT_VERB the whole time.
EXPECTED_BOAT_VERBS = frozenset(
	{
		# Gate 1 — the original ten (snapshot family, backup, image, host-keys, reset).
		"snapshot-vm",
		"snapshot-stop-vm",
		"warm-snapshot-vm",
		"delete-snapshot-vm",
		"upload-snapshot-s3",
		"restore-snapshot-s3",
		"sync-image",
		"promote-snapshot-image",
		"regenerate-host-keys-vm",
		"reset-server",
		# Gate 2 — provision + firewall/tunnel/traffic/wake/export.
		"provision-vm",
		"firewall-apply",
		"vm-tunnel",
		"poll-vm-traffic",
		"probe-woken-vms",
		"export-cleanup-source",
		# The BOAT_ONLY host-prep rename (bootstrap-server -> `boat bootstrap`).
		"bootstrap",
	}
)

# A variables dict that exercises every `_variables_to_flags` branch — a plain
# value, a list (repeated flag), an empty (dropped), and a value with an internal
# space (one shell-quoted token). The flag rendering must be identical whichever
# transport runs the verb, so one representative dict proves the whole swap.
_SAMPLE_VARIABLES = {
	"VIRTUAL_MACHINE_NAME": "uuid-1",
	"SNAPSHOT_ROOTFS_PATH": "/dev/atlas/x",
	"CGROUP_ARG": ["memory.max=1", "cpu.max=200000 100000"],
	"OPTIONAL_EMPTY": "",
}

# Per-builder inputs for the few Fake result builders that read a variable. Boat
# verbs whose builder ignores its argument default to {}. VMS_JSON means two
# different shapes to the two pollers (dicts vs bare uuids), so they cannot share.
_BUILDER_SAMPLE_VARIABLES = {
	"snapshot-vm": {"DISK_GB": "4", "DATA_SNAPSHOT_ROOTFS_PATH": "/dev/atlas/data"},
	"warm-snapshot-vm": {"DISK_GB": "4"},
	"poll-vm-traffic": {"VMS_JSON": json.dumps([{"name": "uuid-a", "ipv6": "2001:db8::2"}])},
	"probe-woken-vms": {"VMS_JSON": json.dumps(["uuid-a", "uuid-b"])},
	"upload-snapshot-s3": {
		"OBJECTS_JSON": json.dumps(
			[{"name": "root", "object_name": "vm/root.img", "disk_gigabytes": 4}]
		)
	},
	"restore-snapshot-s3": {"OBJECTS_JSON": json.dumps([{"name": "root"}])},
	"migration-export-source": {"NBD_PORT": "10123", "DISK_GB": "4"},
}

# The load-bearing keys a controller reads back off each verb's typed result. Not
# exhaustive — enough that a builder silently losing the field the controller keys
# on fails here rather than on a host.
_REQUIRED_RESULT_KEYS = {
	"bootstrap": {"firecracker_version", "kernel_version", "architecture"},
	"bootstrap-server": {"firecracker_version", "kernel_version", "architecture"},
	"snapshot-vm": {"size_bytes", "data_size_bytes"},
	"snapshot-stop-vm": {"memory_snapshot"},
	"sleep-vm": {"memory_snapshot"},
	"warm-snapshot-vm": {"size_bytes", "memory_bytes", "host_signature"},
	"upload-snapshot-s3": {"objects", "total_compressed_bytes"},
	"restore-snapshot-s3": {"objects"},
	"poll-vm-traffic": {"counters"},
	"probe-woken-vms": {"woken"},
	"migration-export-source": {"nbd_port", "root_size_bytes"},
	"migration-export-base": {"base_size_bytes"},
	"migration-poll-hydration": {"hydration_percent", "source_healthy"},
}


class TestBoatVerbSet(unittest.TestCase):
	def test_boat_verbs_are_exactly_the_expected_set(self) -> None:
		# The anchor. Deleting a verb's file must move it between the catalog's two
		# sets, never change WHICH verbs Boat serves.
		self.assertEqual(set(scripts_catalog.BOAT_VERBS), set(EXPECTED_BOAT_VERBS))

	def test_boat_verbs_is_the_union_of_the_two_sets(self) -> None:
		self.assertEqual(
			set(scripts_catalog.BOAT_VERBS),
			set(scripts_catalog._PORTED_VERBS) | set(scripts_catalog.BOAT_ONLY_VERBS),
		)

	def test_the_two_sets_are_disjoint(self) -> None:
		# A verb has an oracle on disk XOR it does not — it cannot be in both.
		self.assertEqual(
			set(scripts_catalog._PORTED_VERBS) & set(scripts_catalog.BOAT_ONLY_VERBS), set()
		)

	def test_runs_on_boat_covers_exactly_the_boat_verbs(self) -> None:
		for verb in EXPECTED_BOAT_VERBS:
			self.assertTrue(scripts_catalog.runs_on_boat(verb), verb)
		# And nothing outside the set routes to boat: a lifecycle verb dispatched
		# through run_boat_task (start-vm), a shell verb, a controller-only verb.
		for verb in ("start-vm", "stop-vm", "reboot-server", "issue-cert", "resize-vm"):
			self.assertFalse(scripts_catalog.runs_on_boat(verb), verb)


class TestBoatVerbFlagCompatibility(unittest.TestCase):
	"""Every BOAT_VERB renders `boat <verb> <flags>` where <flags> is exactly what
	an `atlas <verb>` rendering would carry — the first word is the only change."""

	def test_every_boat_verb_renders_boat_with_identical_flags(self) -> None:
		flags = runner._variables_to_flags(_SAMPLE_VARIABLES)
		for verb in scripts_catalog.BOAT_VERBS:
			with self.subTest(verb=verb):
				command = runner._remote_command(verb, None, _SAMPLE_VARIABLES)
				first, remainder = command.split(" ", 1)
				# The swap: first word is `boat` …
				self.assertEqual(first, "boat")
				# … and everything after it is byte-for-byte the `atlas <verb> <flags>`
				# tail — the flags the controller passes render the same either way.
				self.assertEqual(remainder, f"{verb} {flags}".strip())
				# Never the shell or interpreter shape.
				self.assertNotIn("bash -x", command)
				self.assertNotIn("python3", command)
				self.assertNotIn("PYTHONPATH", command)

	def test_the_flag_rendering_is_the_transport_independent_part(self) -> None:
		# A non-boat python verb renders the identical flag tail behind `atlas`, so
		# the boat and atlas commands differ in the first word and nothing else.
		verb = "start-vm"
		self.assertFalse(scripts_catalog.runs_on_boat(verb))
		atlas_command = runner._remote_command(verb, None, {"VIRTUAL_MACHINE_NAME": "u", "X": ["a", "b"]})
		self.assertEqual(atlas_command, "atlas start-vm --virtual-machine-name u --x a --x b")


class TestBoatOnlyVerbsHaveNoFile(unittest.TestCase):
	"""A BOAT_ONLY verb answers every file-derived question without a file."""

	def test_boat_only_verbs_are_fileless_but_flag_shaped(self) -> None:
		for verb in scripts_catalog.BOAT_ONLY_VERBS:
			with self.subTest(verb=verb):
				self.assertTrue(scripts_catalog.runs_on_boat(verb), verb)
				# kind() is asked for a command SHAPE, not an interpreter: a boat verb's
				# shape is the flag-taking "python" one, decided without reading a file.
				self.assertEqual(scripts_catalog.kind(verb), "python", verb)
				# Nothing on disk, nothing shipped durably.
				self.assertIsNone(scripts_catalog.durable_remote_path(verb), verb)
				self.assertNotIn(verb, scripts_catalog.host_task_scripts(), verb)
				with self.assertRaises(FileNotFoundError):
					scripts_catalog.file_for(verb)


class TestPortedVerbsKeepTheirOracle(unittest.TestCase):
	"""While a verb is still in _PORTED_VERBS its `.py` oracle is on disk. A verb
	leaves the set the moment its file is deleted, so this holds at every gate —
	and once every file is gone the set is empty and this is vacuously true."""

	def test_every_ported_verb_has_a_python_file_on_disk(self) -> None:
		for verb in scripts_catalog._PORTED_VERBS:
			with self.subTest(verb=verb):
				self.assertTrue(scripts_catalog.file_for(verb).endswith(".py"), verb)
				self.assertEqual(scripts_catalog.kind(verb), "python", verb)
				self.assertEqual(
					scripts_catalog.durable_remote_path(verb),
					f"{scripts_catalog.DURABLE_SCRIPT_DIRECTORY}/{scripts_catalog.file_for(verb)}",
					verb,
				)


class TestFakeResultShapesSurvive(unittest.TestCase):
	"""The typed result the controller parses back is synthesized in fake_tasks,
	independent of any `.py`. Every builder must keep producing a shape that
	round-trips through the `ATLAS_RESULT=` marker parse_result reads."""

	def test_every_fake_result_builder_produces_a_parseable_shape(self) -> None:
		for verb, builder in fake_tasks._RESULT_BUILDERS.items():
			with self.subTest(verb=verb):
				result = builder(_BUILDER_SAMPLE_VARIABLES.get(verb, {}))
				self.assertIsInstance(result, dict, verb)
				# The one line the controller scrapes back must round-trip to the dict.
				self.assertEqual(parse_result(result_line(result)), result, verb)
				required = _REQUIRED_RESULT_KEYS.get(verb)
				if required is not None:
					self.assertTrue(required.issubset(result), (verb, required - set(result)))

	def test_the_boat_verbs_that_return_a_result_are_covered(self) -> None:
		# The BOAT_VERBS whose controller reads a typed result all keep a Fake
		# builder — the synthesis that stands in for the deleted script on a Fake
		# host. (Verbs with no typed result — reset-server, firewall-apply — need no
		# builder; they emit a plain line.)
		for verb in ("snapshot-vm", "snapshot-stop-vm", "warm-snapshot-vm", "upload-snapshot-s3"):
			self.assertIn(verb, fake_tasks._RESULT_BUILDERS, verb)
