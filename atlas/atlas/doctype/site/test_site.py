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
	# source of truth); Root Domain denormalizes all three at insert (the FQDN
	# suffix still comes from the active Root Domain).
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
			patch.object(
				site_module,
				"_deploy_site",
				return_value={
					"site": site_name,
					"serving": True,
					"login_url": f"https://{site_name}/app?sid=tok",
				},
			) as m_deploy,
			patch.object(site_module, "_wait_for_http") as m_http,
			patch.object(site_module, "_create_subdomain", return_value="sub-1") as m_sub,
			patch.object(
				site_module, "_provision_pilot", return_value="acme-pilot.blr1.frappe.dev"
			) as m_pilot,
			patch.object(site_module.frappe.db, "commit"),
		):
			site_module.auto_provision(site_name)
		return {
			"prov": m_prov,
			"wait": m_wait,
			"deploy": m_deploy,
			"http": m_http,
			"sub": m_sub,
			"pilot": m_pilot,
		}

	def test_happy_path_reaches_running(self) -> None:
		site = _new_site("acme")
		mocks = self._run_with_mocks(site.name)
		site.reload()
		self.assertEqual(site.status, "Running")
		self.assertEqual(site.virtual_machine, "cloned-vm")
		self.assertEqual(site.subdomain_doc, "sub-1")
		# The owner is handed the one-click login URL the deploy minted (`bench
		# browse --sid`) — NOT a password. Stored on the row by the controller from
		# the deploy's parsed result.
		self.assertEqual(site.login_url, f"https://{site.name}/app?sid=tok")
		# The login URL is a real 24h `bench browse --sid` session, so the controller
		# stamps when it stops working — Central regenerates a fresh one for a late
		# click. Stamped iff the URL is (both written in the same step).
		self.assertTrue(site.login_url_expires_at)
		# The whole chain fired, in order.
		mocks["prov"].assert_called_once()
		mocks["wait"].assert_called_once_with("cloned-vm")
		mocks["deploy"].assert_called_once()
		# wait_for_http gets the Site (for the FQDN Host header) and the VM name.
		http_args = mocks["http"].call_args.args
		self.assertEqual(http_args[1], "cloned-vm")
		mocks["sub"].assert_called_once()
		# The attached Pilot admin console was stood up on the SAME backing VM (the
		# front door Central's Asset resolves for "Open") — see spec/14-self-serve.md.
		# (auto_provision passes its own re-fetched Site doc, so assert on the args:
		# the Site name + the just-cloned VM name.)
		mocks["pilot"].assert_called_once()
		pilot_args = mocks["pilot"].call_args.args
		self.assertEqual(pilot_args[0].name, site.name)
		self.assertEqual(pilot_args[1], "cloned-vm")
		# Each phase transition stamped its start time (drives the status page's
		# per-phase timing). All three real phases were entered, so all three carry
		# a stamp, in non-decreasing order.
		stamps = [site.provisioning_started, site.deploying_started, site.running_started]
		self.assertTrue(all(stamps), f"a phase entry left no timestamp: {stamps}")
		self.assertEqual(stamps, sorted(stamps))

	def test_regenerate_login_url_remints_and_restamps(self) -> None:
		# A Running site whose stored login URL Central asks to refresh: re-mint in the
		# guest (mocked seam), re-stamp login_url + a fresh expiry, and return the mirror.
		site = _new_site("acme")
		self._run_with_mocks(site.name)
		site.reload()
		old_expiry = site.login_url_expires_at
		fresh = f"https://{site.name}/app?sid=fresh"
		with (
			patch.object(site_module, "_regenerate_login", return_value={"login_url": fresh}) as m_regen,
			patch.object(site_module.frappe.db, "commit"),
		):
			mirror = site.regenerate_login_url()
		m_regen.assert_called_once_with(site, site.virtual_machine)
		site.reload()
		self.assertEqual(site.login_url, fresh)
		self.assertTrue(site.login_url_expires_at)
		self.assertGreaterEqual(site.login_url_expires_at, old_expiry)
		# The returned mirror is what Central re-reads — it carries the fresh URL.
		self.assertEqual(mirror["login_url"], fresh)
		self.assertEqual(mirror["url"], f"https://{site.name}")

	def test_regenerate_login_url_refused_before_running(self) -> None:
		# Nothing to sign into before the site serves — the guard fails loud.
		site = _new_site("acme")
		self.assertEqual(site.status, "Pending")
		with self.assertRaises(frappe.ValidationError):
			site.regenerate_login_url()

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
			patch.object(
				site_module,
				"_deploy_site",
				return_value={"login_url": "https://acme.blr1.frappe.dev/app?sid=tok"},
			),
			patch.object(site_module, "_wait_for_http"),
			patch.object(site_module, "_create_subdomain", return_value="sub-1"),
			patch.object(site_module, "_provision_pilot", return_value="acme-pilot.blr1.frappe.dev"),
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
			patch.object(
				site_module,
				"_deploy_site",
				return_value={"login_url": f"https://{site.name}/app?sid=tok"},
			),
			patch.object(site_module, "_wait_for_http"),
			patch.object(site_module, "_provision_pilot", return_value="acme-pilot.blr1.frappe.dev"),
			patch.object(site_module.frappe.db, "commit"),
		):
			site_module.auto_provision(site.name)
		site.reload()
		self.assertEqual(site.status, "Running")
		subdomain = frappe.get_doc("Subdomain", site.subdomain_doc)
		self.assertEqual(subdomain.subdomain, "acme")
		self.assertEqual(subdomain.virtual_machine, vm.name)


class TestSiteFakeProvisionStages(IntegrationTestCase):
	"""The deploy + HTTP-probe stages short-circuit on a Fake-backed VM (the same
	`is_fake_server` gate `run_task` uses). A Fake VM carries a documentation IP that
	never answers SSH or HTTP, so without this the real flow times out and the Site is
	left Failed with no Subdomain — the whole point is that a developer_mode/Fake
	auto_provision still creates EVERY record a real one does, only skipping the two
	stages that physically can't run. A real (non-Fake) VM still calls through."""

	def setUp(self) -> None:
		_ensure_root_domain()
		from atlas.tests.fixtures import make_image, make_provider_row, make_server, make_virtual_machine

		for name in frappe.get_all("Site", pluck="name"):
			frappe.delete_doc("Site", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Subdomain", pluck="name"):
			frappe.delete_doc("Subdomain", name, force=1, ignore_permissions=True)
		image = make_image("fake-stage-image")
		fake_server = make_server(
			make_provider_row("fake-stage-provider", provider_type="Fake"),
			title="fake-stage-server",
			provider_type="Fake",
			status="Active",
		)
		self.fake_vm = make_virtual_machine(
			fake_server, image, title="fake-stage-vm", ipv6_address="2001:db8:f::1"
		)
		real_server = make_server(title="real-stage-server", ipv6_address="2001:db8:9::1")
		self.real_vm = make_virtual_machine(
			real_server, image, title="real-stage-vm", ipv6_address="2001:db8:9::5"
		)
		self.site = _new_site("acme")

	def test_deploy_site_noops_on_fake_vm(self) -> None:
		from atlas.atlas import deploy_site as deploy_module

		with patch.object(deploy_module, "deploy_site") as m_deploy:
			result = site_module._deploy_site(self.site, self.fake_vm.name)
		m_deploy.assert_not_called()
		# A synthesized placeholder login_url keeps the mirror shape stable for
		# e2e/desk tests that run against a Fake server.
		self.assertTrue(result["login_url"])

	def test_wait_for_http_noops_on_fake_vm(self) -> None:
		from atlas.atlas import deploy_site as deploy_module

		with patch.object(deploy_module, "wait_for_http") as m_http:
			site_module._wait_for_http(self.site, self.fake_vm.name)
		m_http.assert_not_called()

	def test_deploy_site_calls_through_on_real_vm(self) -> None:
		from atlas.atlas import deploy_site as deploy_module

		with patch.object(deploy_module, "deploy_site") as m_deploy:
			site_module._deploy_site(self.site, self.real_vm.name)
		# The two trailing positional args are the bench-level Central endpoint + token,
		# threaded from create_site; `admin_domain` (the attached Pilot's FQDN) is None here
		# since this unit test calls `_deploy_site` directly without resolving one.
		m_deploy.assert_called_once_with(self.real_vm.name, self.site.name, None, None, admin_domain=None)

	def test_wait_for_http_calls_through_on_real_vm(self) -> None:
		from atlas.atlas import deploy_site as deploy_module

		with patch.object(deploy_module, "wait_for_http") as m_http:
			site_module._wait_for_http(self.site, self.real_vm.name)
		m_http.assert_called_once()


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

	def test_clone_title_is_the_bare_subdomain_label_not_the_fqdn(self) -> None:
		"""The clone's `title` MUST be the bare subdomain label (`acme`), NOT the
		Site's FQDN name (`acme.blr1.frappe.dev`) — a VM title is a plain descriptive
		handle, and the bare label keeps it a clean single token (matching the
		subdomain the Site is fronted at). The warm path passes `title` from the same
		`site.subdomain` expression, so the cold path guards both."""
		site = _new_site("acme")
		self.assertEqual(site.name, "acme.blr1.frappe.dev")
		ctx, recorded = self._record_clone("cold-clone")
		with ctx:
			site_module._provision_backing_vm(site)
		self.assertEqual(recorded[0]["kwargs"]["title"], "acme")


class TestPilotSubdomainFor(IntegrationTestCase):
	"""`pilot_subdomain_for` derives the attached-Pilot label from a site's label:
	`acme` → `acme-pilot`, disambiguated on collision (spec/14-self-serve.md)."""

	def setUp(self) -> None:
		_ensure_root_domain()
		for name in frappe.get_all("Pilot", pluck="name"):
			frappe.delete_doc("Pilot", name, force=1, ignore_permissions=True)

	def test_appends_pilot_suffix(self) -> None:
		from atlas.atlas.subdomain_label import pilot_subdomain_for

		self.assertEqual(pilot_subdomain_for("acme"), "acme-pilot")

	def test_collision_appends_random_tail(self) -> None:
		"""When `<label>-pilot` already backs a Pilot, a short random tail disambiguates
		so a re-created / colliding site still gets a unique console name."""
		from atlas.atlas.doctype.pilot import pilot as pilot_module
		from atlas.atlas.subdomain_label import PILOT_SUFFIX, pilot_subdomain_for

		# Stand up a Pilot at `acme-pilot.<domain>` so the base name is taken. Patch the
		# own-VM provisioner + enqueue so the row exists without a real backing VM.
		with (
			patch.object(pilot_module, "_provision_backing_vm", return_value="stub-vm"),
			patch.object(pilot_module.frappe, "enqueue"),
		):
			frappe.get_doc({"doctype": "Pilot", "subdomain": "acme-pilot"}).insert(ignore_permissions=True)
		label = pilot_subdomain_for("acme")
		self.assertNotEqual(label, "acme-pilot")
		self.assertTrue(label.startswith("acme" + PILOT_SUFFIX + "-"))
		# The result is still a valid Contract-A label.
		from atlas.atlas.subdomain_label import validate_label

		validate_label(label)


class TestSitePilotAttachment(IntegrationTestCase):
	"""create_site stands up an attached Pilot admin console on the site's OWN backing
	VM (spec/14-self-serve.md): `<subdomain>-pilot.<region>` → the same VM, so Central's
	Asset "Open" resolves a bench console (front_door_for_vm prefers Pilot), not the
	customer site. The deploy short-circuits on the Fake VM, so the real `_provision_pilot`
	orchestration (create the Pilot, attach it, mint, route, mark Running, link back) runs
	hostless here."""

	def setUp(self) -> None:
		_ensure_root_domain()
		from atlas.tests.fixtures import make_image, make_provider_row, make_server, make_virtual_machine

		for name in frappe.get_all("Site", pluck="name"):
			frappe.delete_doc("Site", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Pilot", pluck="name"):
			frappe.delete_doc("Pilot", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Subdomain", pluck="name"):
			frappe.delete_doc("Subdomain", name, force=1, ignore_permissions=True)
		image = make_image("pilot-attach-image")
		fake_server = make_server(
			make_provider_row("pilot-attach-provider", provider_type="Fake"),
			title="pilot-attach-server",
			provider_type="Fake",
			status="Active",
		)
		self.fake_vm = make_virtual_machine(
			fake_server, image, title="pilot-attach-vm", ipv6_address="2001:db8:f::9"
		)
		self.site = _new_site("acme")
		# deploy_attached commits on its FAILURE path (so a Failed pilot survives the job
		# rollback, like the other auto_provision funcs). In-test that commit would leak
		# rows past IntegrationTestCase's per-test rollback and poison the shared DB with a
		# committed `acme` row that later `delete_doc`s deadlock on (FOR UPDATE NOWAIT). Mock
		# commit to a no-op for the whole class — the same discipline TestPilot._drive_provision
		# uses. Rows then roll back cleanly between tests.
		from atlas.atlas.doctype.pilot import pilot as pilot_module

		self._commit_patches = [
			patch.object(site_module.frappe.db, "commit"),
			patch.object(pilot_module.frappe.db, "commit"),
		]
		for p in self._commit_patches:
			p.start()
		self.addCleanup(lambda: [p.stop() for p in self._commit_patches])

	def test_provision_pilot_attaches_console_to_same_vm(self) -> None:
		pilot_name = site_module._provision_pilot(self.site, self.fake_vm.name, "acme-pilot")
		# Named `<subdomain>-pilot.<region>` and linked back on the Site.
		self.assertEqual(pilot_name, "acme-pilot.blr1.frappe.dev")
		self.site.reload()
		self.assertEqual(self.site.pilot, pilot_name)
		pilot = frappe.get_doc("Pilot", pilot_name)
		# Attached: bound to the site's VM, admin build_mode, and Running with a login URL.
		self.assertTrue(pilot.attached)
		self.assertEqual(pilot.virtual_machine, self.fake_vm.name)
		self.assertEqual(pilot.build_mode, "admin")
		self.assertEqual(pilot.status, "Running")
		self.assertTrue(pilot.login_url)
		# The Pilot's own Subdomain routes `acme-pilot` → the SAME backing VM /128.
		self.assertTrue(pilot.subdomain_doc)
		pilot_sub = frappe.get_doc("Subdomain", pilot.subdomain_doc)
		self.assertEqual(pilot_sub.subdomain, "acme-pilot")
		self.assertEqual(pilot_sub.virtual_machine, self.fake_vm.name)

	def test_attached_pilot_does_not_create_or_boot_its_own_vm(self) -> None:
		# The attach path must NOT provision a VM or enqueue a boot job (the Site owns
		# the VM). Assert the module's own-VM provisioner is never touched.
		from atlas.atlas.doctype.pilot import pilot as pilot_module

		with patch.object(pilot_module, "_provision_backing_vm") as m_prov:
			site_module._provision_pilot(self.site, self.fake_vm.name, "acme-pilot")
		m_prov.assert_not_called()

	def test_attached_pilot_terminate_does_not_touch_the_vm(self) -> None:
		# The attached Pilot's own terminate() must NOT terminate the shared VM (the Site
		# owns it) — the `.attached` guard makes _terminate_backing_vm a no-op. Terminate
		# the Pilot alone and assert the VM is untouched (only the Site would terminate it).
		site_module._provision_pilot(self.site, self.fake_vm.name, "acme-pilot")
		pilot = frappe.get_doc("Pilot", self.site.pilot)
		pilot.terminate()
		pilot.reload()
		self.assertEqual(pilot.status, "Terminated")
		self.assertFalse(frappe.db.exists("Subdomain", pilot.get("subdomain_doc")))
		# The shared VM is NOT terminated by the Pilot (attached guard).
		self.assertNotEqual(frappe.db.get_value("Virtual Machine", self.fake_vm.name, "status"), "Terminated")

	def test_terminate_cascades_to_pilot_and_vm_once(self) -> None:
		# Full cascade: Site.terminate → Pilot Terminated + VM Terminated (once each).
		site_module._provision_pilot(self.site, self.fake_vm.name, "acme-pilot")
		self.site.db_set("virtual_machine", self.fake_vm.name)
		self.site.reload()
		pilot_name = self.site.pilot
		self.site.terminate()
		self.site.reload()
		self.assertEqual(self.site.status, "Terminated")
		self.assertEqual(frappe.db.get_value("Pilot", pilot_name, "status"), "Terminated")
		self.assertEqual(frappe.db.get_value("Virtual Machine", self.fake_vm.name, "status"), "Terminated")


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
