"""Unit tests for the Site controller — the routing-string contract (Contract A),
immutability, the provision→deploy→running state machine and its background
orchestration (Contract B), and terminate. All milliseconds, no host: the host
parts (real clone + deploy + HTTP 200) are proven in the e2e (spec/14-self-serve.md).
Sites are operator/Central-owned now (no end-user owner scoping); the Central-facing
create_site/get_site API has its own test module (test_api_site.py).

The background entrypoint's host steps — clone the VM, wait for SSH, run
deploy-site.py, wait for HTTP 200 — are mocked here at the module seams; only the
pure orchestration (status transitions, Subdomain creation, fail-loud) is
asserted."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.site import site as site_module
from atlas.atlas.doctype.virtual_machine_snapshot.virtual_machine_snapshot import VirtualMachineSnapshot
from atlas.tests.fixtures import make_provider, make_server

ROOT_DOMAIN = "blr1.frappe.dev"
REGION = "blr1"
SNAPSHOT_NAME = "golden-bench-snap"


def _ensure_root_domain() -> None:
	# Region + active DNS / TLS vendor types live on Atlas Settings (the single
	# source of truth); Site reads region from it and Root Domain denormalizes all
	# three at insert.
	frappe.db.set_single_value("Atlas Settings", "region", REGION)
	frappe.db.set_single_value("Atlas Settings", "dns_provider_type", "Route53")
	frappe.db.set_single_value("Atlas Settings", "tls_provider_type", "Let's Encrypt")
	if not frappe.db.exists("Root Domain", ROOT_DOMAIN):
		frappe.get_doc(
			{
				"doctype": "Root Domain",
				"domain": ROOT_DOMAIN,
				"region": REGION,
				"is_active": 1,
				"dns_provider_type": "Route53",
				"tls_provider_type": "Let's Encrypt",
			}
		).insert(ignore_permissions=True)
	# Site placement resolves THE single active Root Domain, so other tests'
	# leftover active rows (test_root_domain seeds nyc3/blr1; the e2e config
	# seeds atlas1.x) would make resolution ambiguous. Deactivate everything but
	# ours for the duration of these tests (rolled back with the transaction).
	frappe.db.set_value("Root Domain", ROOT_DOMAIN, "is_active", 1)
	for name in frappe.get_all("Root Domain", filters={"is_active": 1}, pluck="name"):
		if name != ROOT_DOMAIN:
			frappe.db.set_value("Root Domain", name, "is_active", 0)


def _ensure_golden_snapshot() -> str:
	"""A backing VM + an Available Virtual Machine Snapshot pointed at by Atlas
	Settings. The clone path is mocked in the orchestration tests, so this row
	only has to exist + be Available for placement.default_bench_snapshot."""
	provider = make_provider("site-test-provider")
	# A Site never runs placement.default_server (it clones from a snapshot whose
	# server is fixed), so this server is deliberately NOT Active — leaving it
	# Pending keeps it out of the placement-capacity tests' Active-server set.
	server = make_server(
		provider,
		"site-test-server",
		ipv6_address="2001:db8:9::1",
		ipv6_prefix="2001:db8:9::/64",
		ipv6_virtual_machine_range="2001:db8:9::/124",
	)
	if not frappe.db.exists("Virtual Machine Snapshot", SNAPSHOT_NAME):
		# A source VM the snapshot belongs to (clone_to_new_vm reads its server).
		from atlas.tests.fixtures import make_image, make_virtual_machine

		image = make_image("site-test-image")
		source_vm = make_virtual_machine(server, image, title="golden-source")
		doc = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "golden bench",
				"virtual_machine": source_vm.name,
				"server": server.name,
				"status": "Available",
				"source_image": image.name,
				"disk_gigabytes": 12,
				"rootfs_path": "/dev/atlas/atlas-snap-golden",
			}
		)
		# Virtual Machine Snapshot autonames `hash` (Random), which ignores
		# __newname — so pin the name explicitly (flags.name_set bypasses autoname)
		# to the stable SNAPSHOT_NAME that Atlas Settings.default_bench_snapshot and
		# the warm-provision tests resolve against.
		doc.name = SNAPSHOT_NAME
		doc.flags.name_set = True
		doc.insert(ignore_permissions=True)
	frappe.db.set_single_value("Atlas Settings", "default_bench_snapshot", SNAPSHOT_NAME)
	frappe.db.set_single_value("Atlas Settings", "ssh_public_key", "ssh-ed25519 AAAAFLEET")
	return SNAPSHOT_NAME


def _new_site(subdomain: str = "acme", **overrides):
	doc = {"doctype": "Site", "subdomain": subdomain}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


class TestSiteRoutingContract(IntegrationTestCase):
	"""Contract A — the one routing string, plus the label / reserved / unique
	validations that gate it."""

	def setUp(self) -> None:
		_ensure_root_domain()
		_ensure_golden_snapshot()
		for name in frappe.get_all("Site", pluck="name"):
			frappe.delete_doc("Site", name, force=1, ignore_permissions=True)

	def test_autoname_is_the_fqdn(self) -> None:
		site = _new_site("acme")
		self.assertEqual(site.name, "acme.blr1.frappe.dev")

	def test_region_resolved_from_active_root_domain(self) -> None:
		site = _new_site("acme")
		self.assertEqual(site.region, REGION)

	def test_starts_pending(self) -> None:
		site = _new_site("acme")
		self.assertEqual(site.status, "Pending")

	def test_rejects_dotted_label(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			_new_site("ac.me")
		self.assertIn("single label", str(raised.exception))

	def test_rejects_uppercase_label(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			_new_site("Acme")
		self.assertIn("lowercase", str(raised.exception))

	def test_rejects_leading_hyphen(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			_new_site("-acme")

	def test_rejects_trailing_hyphen(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			_new_site("acme-")

	def test_rejects_illegal_chars(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			_new_site("ac_me")

	def test_rejects_overlong_label(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			_new_site("a" * 64)

	def test_rejects_reserved_label(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			_new_site("www")
		self.assertIn("reserved", str(raised.exception))

	def test_duplicate_subdomain_is_clean_taken_message(self) -> None:
		_new_site("acme")
		with self.assertRaises(frappe.ValidationError) as raised:
			_new_site("acme")
		self.assertIn("already taken", str(raised.exception))

	def test_no_active_domain_fails_loud(self) -> None:
		frappe.db.set_value("Root Domain", ROOT_DOMAIN, "is_active", 0)
		try:
			with self.assertRaises(frappe.ValidationError) as raised:
				_new_site("acme")
			self.assertIn("No domain is configured", str(raised.exception))
		finally:
			frappe.db.set_value("Root Domain", ROOT_DOMAIN, "is_active", 1)


class TestSiteImmutability(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_root_domain()
		_ensure_golden_snapshot()
		for name in frappe.get_all("Site", pluck="name"):
			frappe.delete_doc("Site", name, force=1, ignore_permissions=True)

	def test_region_immutable(self) -> None:
		site = _new_site("acme")
		site.region = "nyc3"
		with self.assertRaises(frappe.ValidationError) as raised:
			site.save(ignore_permissions=True)
		self.assertIn("region is immutable", str(raised.exception))

	def test_virtual_machine_immutable(self) -> None:
		from atlas.tests.fixtures import make_image, make_virtual_machine

		server = frappe.db.get_value("Server", {"title": "site-test-server"}, "name")
		image = make_image("site-test-image")
		# Two real VMs so the Link-existence check passes and the immutability
		# guard (not the link guard) is what trips.
		vm_a = make_virtual_machine(server, image, title="vm-a")
		vm_b = make_virtual_machine(server, image, title="vm-b")
		site = _new_site("acme")
		site.db_set("virtual_machine", vm_a.name)
		site.reload()
		site.virtual_machine = vm_b.name
		with self.assertRaises(frappe.ValidationError) as raised:
			site.save(ignore_permissions=True)
		self.assertIn("virtual_machine is immutable", str(raised.exception))


class TestSiteOrchestration(IntegrationTestCase):
	"""The provision→deploy→running background flow (Contract B). Host steps are
	mocked at the module seams; the transitions + Subdomain creation are real."""

	def setUp(self) -> None:
		_ensure_root_domain()
		_ensure_golden_snapshot()
		for name in frappe.get_all("Site", pluck="name"):
			frappe.delete_doc("Site", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Subdomain", pluck="name"):
			frappe.delete_doc("Subdomain", name, force=1, ignore_permissions=True)

	def _run_with_mocks(self, site_name: str, *, vm_name: str = "cloned-vm"):
		"""Run auto_provision with every host seam mocked. Returns the patch
		mocks so a test can assert on calls. `frappe.db.commit` is patched to a
		no-op: the real flow commits after the clone (so the clone's own boot job
		can run — the live transaction hand-off), but committing in a unit test
		would leak rows past IntegrationTestCase's auto-rollback and pollute the
		shared test DB."""
		with (
			patch.object(site_module, "_provision_backing_vm", return_value=vm_name) as m_prov,
			patch.object(site_module, "_wait_for_vm_running") as m_wait,
			patch.object(site_module, "_deploy_site") as m_deploy,
			patch.object(site_module, "_wait_for_http") as m_http,
			patch.object(site_module, "_create_subdomain", return_value="sub-1") as m_sub,
			patch.object(site_module.frappe.db, "commit"),
		):
			site_module.auto_provision(site_name)
		return {
			"prov": m_prov,
			"wait": m_wait,
			"deploy": m_deploy,
			"http": m_http,
			"sub": m_sub,
		}

	def test_happy_path_reaches_running(self) -> None:
		site = _new_site("acme")
		mocks = self._run_with_mocks(site.name)
		site.reload()
		self.assertEqual(site.status, "Running")
		self.assertEqual(site.virtual_machine, "cloned-vm")
		self.assertEqual(site.subdomain_doc, "sub-1")
		# The owner is handed the SHARED baked Administrator password (rotated after
		# first login) — stored (encrypted) on the row by the controller, NOT a value
		# the deploy returns (the per-VM reset is gone). It is the build.sh constant.
		self.assertEqual(site.get_password("admin_password"), site_module.BAKED_ADMIN_PASSWORD)
		# The whole chain fired, in order.
		mocks["prov"].assert_called_once()
		mocks["wait"].assert_called_once_with("cloned-vm")
		mocks["deploy"].assert_called_once()
		# wait_for_http gets the Site (for the FQDN Host header) and the VM name.
		http_args = mocks["http"].call_args.args
		self.assertEqual(http_args[1], "cloned-vm")
		mocks["sub"].assert_called_once()
		# Each phase transition stamped its start time (drives the status page's
		# per-phase timing). All three real phases were entered, so all three carry
		# a stamp, in non-decreasing order.
		stamps = [site.provisioning_started, site.deploying_started, site.running_started]
		self.assertTrue(all(stamps), f"a phase entry left no timestamp: {stamps}")
		self.assertEqual(stamps, sorted(stamps))

	def test_deploy_failure_marks_failed_and_raises(self) -> None:
		site = _new_site("acme")
		with (
			patch.object(site_module, "_provision_backing_vm", return_value="cloned-vm"),
			patch.object(site_module, "_wait_for_vm_running"),
			patch.object(site_module, "_deploy_site", side_effect=RuntimeError("deploy broke")),
			patch.object(site_module.frappe.db, "commit"),
		):
			with self.assertRaises(RuntimeError):
				site_module.auto_provision(site.name)
		site.reload()
		self.assertEqual(site.status, "Failed")
		# No Subdomain was created on the failed path.
		self.assertFalse(site.subdomain_doc)
		# The deploy phase was entered (stamped) but never finished — so the page
		# shows it as the broken phase with an elapsed-until-failure time, and the
		# running phase never started (no stamp → no time shown).
		self.assertTrue(site.deploying_started)
		self.assertFalse(site.running_started)

	def test_commits_after_clone_so_boot_job_can_run(self) -> None:
		"""Regression: the clone's boot runs in a SEPARATE after_insert job that
		cannot start until auto_provision commits. If we don't commit after the
		clone, the wait blocks forever and the rollback deletes the clone — the
		'Fulfilled, no VM' deadlock. Assert: commit happens AFTER the VM is set and
		BEFORE the running-wait, and the wait runs after that commit."""
		site = _new_site("acme")
		order = []
		with (
			patch.object(site_module, "_provision_backing_vm", return_value="cloned-vm"),
			patch.object(
				site_module, "_wait_for_vm_running", side_effect=lambda *a, **k: order.append("wait")
			),
			patch.object(site_module, "_deploy_site", return_value="pw"),
			patch.object(site_module, "_wait_for_http"),
			patch.object(site_module, "_create_subdomain", return_value="sub-1"),
			patch.object(site_module.frappe.db, "commit", side_effect=lambda: order.append("commit")),
		):
			site_module.auto_provision(site.name)
		# commit happened, and the boot-wait only ran after a commit (hand-off).
		self.assertIn("commit", order)
		self.assertEqual(order[order.index("wait") - 1], "commit")

	def test_failed_status_is_committed(self) -> None:
		"""Regression: on failure the Failed status must be committed before the
		re-raise, or the job's rollback reverts it to Pending (a stuck Pending is
		indistinguishable from 'never ran')."""
		site = _new_site("acme")
		committed = []
		with (
			patch.object(site_module, "_provision_backing_vm", return_value="cloned-vm"),
			patch.object(site_module, "_wait_for_vm_running", side_effect=RuntimeError("boot broke")),
			patch.object(site_module.frappe.db, "commit", side_effect=lambda: committed.append(True)),
		):
			with self.assertRaises(RuntimeError):
				site_module.auto_provision(site.name)
		# A commit fired on the failure path (the Failed-status commit).
		self.assertTrue(committed)

	def test_no_op_when_not_pending(self) -> None:
		site = _new_site("acme")
		site.db_set("status", "Running")
		# Should return immediately without touching any seam.
		with patch.object(site_module, "_provision_backing_vm") as m_prov:
			site_module.auto_provision(site.name)
		m_prov.assert_not_called()

	def test_create_subdomain_carries_routing_identity(self) -> None:
		"""The real _create_subdomain (not mocked) builds a Subdomain whose
		fields flow straight from the Site — Contract A, no transformation."""
		site = _new_site("acme")
		# Give the site a backing VM with an ipv6 so Subdomain's address
		# denormalization succeeds.
		from atlas.tests.fixtures import make_image, make_virtual_machine

		server = frappe.db.get_value("Server", {"title": "site-test-server"}, "name")
		image = make_image("site-test-image")
		vm = make_virtual_machine(server, image, title="acme-backing")
		with (
			patch.object(site_module, "_provision_backing_vm", return_value=vm.name),
			patch.object(site_module, "_wait_for_vm_running"),
			patch.object(site_module, "_deploy_site"),
			patch.object(site_module, "_wait_for_http"),
			patch.object(site_module.frappe.db, "commit"),
		):
			site_module.auto_provision(site.name)
		site.reload()
		self.assertEqual(site.status, "Running")
		subdomain = frappe.get_doc("Subdomain", site.subdomain_doc)
		self.assertEqual(subdomain.subdomain, "acme")
		self.assertEqual(subdomain.region, REGION)
		self.assertEqual(subdomain.virtual_machine, vm.name)


class TestSiteWarmFirstProvision(IntegrationTestCase):
	"""The warm-first backing-VM selection (spec/14-self-serve.md § Warm-first
	provisioning). `_provision_backing_vm` resolves the cold golden, then — when
	its server carries an Available kind=Warm golden — RESUMES the warm one
	instead of cold-booting. This is the only place `clone_to_new_vm` is invoked
	from the Site layer, so the warm-vs-cold dispatch and the captured-size
	discipline are asserted here (host facts — a real restore — are in the
	`warm_restore` e2e). The clone itself is mocked: this is the pure selection
	logic, no host."""

	def setUp(self) -> None:
		_ensure_root_domain()
		_ensure_golden_snapshot()
		# Clear leftover Sites/Subdomains (same as the other Site test classes) so the
		# "acme" label is free. Warm Snapshot rows are NOT cleaned up here: per-test
		# rollback drops them, and deleting one would fire its real on_trash SSH
		# teardown.
		for name in frappe.get_all("Site", pluck="name"):
			frappe.delete_doc("Site", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Subdomain", pluck="name"):
			frappe.delete_doc("Subdomain", name, force=1, ignore_permissions=True)

	def _make_warm_snapshot(self) -> str:
		"""A kind=Warm Available snapshot on the SAME server as the cold golden, so
		placement.warm_bench_snapshot_for_server (per-server) finds it. It mirrors
		the cold golden's server/source-VM/image so it is a valid sibling row.
		Returns the (hash-autonamed) row name the warm lookup will resolve to."""
		cold = frappe.get_doc("Virtual Machine Snapshot", SNAPSHOT_NAME)
		warm = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "golden bench (warm)",
				"kind": "Warm",
				"virtual_machine": cold.virtual_machine,
				"server": cold.server,
				"status": "Available",
				"source_image": cold.source_image,
				"disk_gigabytes": cold.disk_gigabytes,
				"rootfs_path": "/dev/atlas/atlas-snap-warm",
			}
		).insert(ignore_permissions=True)
		return warm.name

	def _record_clone(self, return_value: str):
		"""Patch clone_to_new_vm to record which snapshot it was called on (self.name)
		and with what kwargs, without doing the real host clone. Returns the patch
		context manager and a one-element list the recorded call lands in."""
		recorded: list[dict] = []

		def fake_clone(snapshot_self, **kwargs):
			recorded.append({"snapshot": snapshot_self.name, "kwargs": kwargs})
			return return_value

		ctx = patch.object(VirtualMachineSnapshot, "clone_to_new_vm", autospec=True, side_effect=fake_clone)
		return ctx, recorded

	def test_cold_path_clones_cold_golden_with_explicit_tier_size(self) -> None:
		"""No warm row on the server → today's exact cold path: clone the cold
		golden, passing the full explicit tier size (vcpus + cpu cap + memory)."""
		site = _new_site("acme")
		ctx, recorded = self._record_clone("cold-clone")
		with ctx:
			vm_name = site_module._provision_backing_vm(site)
		self.assertEqual(vm_name, "cold-clone")
		# Clone was the COLD golden, at the explicit Shared 4x tier.
		self.assertEqual(recorded[0]["snapshot"], SNAPSHOT_NAME)
		kw = recorded[0]["kwargs"]
		self.assertEqual(kw["vcpus"], site_module.SITE_VM_SIZE["vcpus"])
		self.assertEqual(kw["memory_megabytes"], site_module.SITE_VM_SIZE["memory_megabytes"])
		self.assertEqual(kw["cpu_max_cores"], site_module.SITE_VM_SIZE["cpu_max_cores"])

	def test_warm_path_resumes_warm_golden(self) -> None:
		"""An Available warm golden on the server → resume it (clone the WARM
		snapshot, not the cold one)."""
		warm = self._make_warm_snapshot()
		site = _new_site("acme")
		ctx, recorded = self._record_clone("warm-clone")
		with ctx:
			vm_name = site_module._provision_backing_vm(site)
		self.assertEqual(vm_name, "warm-clone")
		self.assertEqual(recorded[0]["snapshot"], warm)

	def test_warm_clone_passes_only_cpu_cap_not_frozen_size(self) -> None:
		"""A warm restore comes up at the CAPTURED vcpus/memory (the frozen vmstate
		pins them — clone_to_new_vm rejects overrides), so only the host-side cgroup
		cpu_max_cores is passed; vcpus and memory_megabytes are NOT."""
		self._make_warm_snapshot()
		site = _new_site("acme")
		ctx, recorded = self._record_clone("warm-clone")
		with ctx:
			site_module._provision_backing_vm(site)
		kw = recorded[0]["kwargs"]
		self.assertEqual(kw["cpu_max_cores"], site_module.SITE_VM_SIZE["cpu_max_cores"])
		self.assertNotIn("vcpus", kw)
		self.assertNotIn("memory_megabytes", kw)


class TestSiteTerminate(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_root_domain()
		_ensure_golden_snapshot()
		for name in frappe.get_all("Site", pluck="name"):
			frappe.delete_doc("Site", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Subdomain", pluck="name"):
			frappe.delete_doc("Subdomain", name, force=1, ignore_permissions=True)

	def test_terminate_marks_terminated(self) -> None:
		site = _new_site("acme")
		site.terminate()
		site.reload()
		self.assertEqual(site.status, "Terminated")

	def test_terminate_twice_raises(self) -> None:
		site = _new_site("acme")
		site.terminate()
		with self.assertRaises(frappe.ValidationError) as raised:
			site.terminate()
		self.assertIn("already terminated", str(raised.exception))

	def test_terminate_deletes_subdomain_and_terminates_vm(self) -> None:
		from unittest.mock import patch as _patch

		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module
		from atlas.tests._mocks import fake_task
		from atlas.tests.fixtures import make_image, make_virtual_machine

		site = _new_site("acme")
		server = frappe.db.get_value("Server", {"title": "site-test-server"}, "name")
		image = make_image("site-test-image")
		vm = make_virtual_machine(server, image, title="acme-backing")
		vm.db_set("status", "Running")
		subdomain = frappe.get_doc(
			{
				"doctype": "Subdomain",
				"subdomain": "acme",
				"region": REGION,
				"virtual_machine": vm.name,
				"active": 1,
			}
		).insert(ignore_permissions=True)
		site.db_set("virtual_machine", vm.name)
		site.db_set("subdomain_doc", subdomain.name)
		site.reload()
		with _patch.object(vm_module, "run_task", return_value=fake_task(name="task-term-site")):
			site.terminate()
		site.reload()
		self.assertEqual(site.status, "Terminated")
		self.assertFalse(frappe.db.exists("Subdomain", subdomain.name), "Subdomain deleted on terminate")
		self.assertEqual(
			frappe.db.get_value("Virtual Machine", vm.name, "status"),
			"Terminated",
			"backing VM terminated",
		)
