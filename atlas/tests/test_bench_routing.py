"""Unit tests for self-service subdomain routing — the one-way push model (spec/18).

Covers the controller side end to end with NO host:

- Caller resolution: all four endpoints resolve the VM from `frappe.local.request_ip`
  (the edge-supplied source /128) against `Virtual Machine.ipv6_address`, take NO
  `vm_uuid` parameter (a body param is ignored), and reject a source matching no VM /
  a Terminated VM / a proxy with no write (and, for `list`, no inventory). The
  leftmost-XFF forgery must NOT resolve to the named victim under the trusted-edge
  contract.
- `register` (the reserve-first gate): the full rule chain in order, `active=1` on ok,
  `taken` on an owned label AND a `DuplicateEntryError` race, `reserved`/denylist,
  `at_limit` at cap, `invalid`; the inserted row's `virtual_machine` is the
  source-resolved VM; idempotent re-register of an owned label.
- `deregister` (drop + create-failure rollback): deletes only the caller's own row,
  another VM's row is a no-op, idempotent on an absent row, fires `on_trash`.
- `check_label` status mapping + suffix; `list` own-rows + controller-built fqdn +
  empty `{"domains": []}` + clean throw on a non-resolving source.
- Component G (cap): the tier lookup + register admits up to cap then `at_limit`.
- Component H (denylist DocType): both endpoints reject an enabled row; `enabled=0`
  lifts the block immediately; a runtime row is honored on the next call.
- Component I (audit): every endpoint writes a `Bench Routing Audit` row on the ok AND
  reject path; the table is MyISAM; the row survives a request rollback; a non-resolving
  source records blank vm + the spoofing source_ip; the helper does not commit.
- Component F: `VirtualMachine.terminate()` deletes every Subdomain; there is NO
  sweeper (the scheduler carries no `reconcile_*`/`sweep_*` entry).

`frappe.local.request_ip` is set directly per test (the edge is a host fact; the unit
boundary is "given this request_ip, the right VM or a reject"). `frappe.enqueue` is
mocked where the enqueue itself is the assertion.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import bench_routing
from atlas.atlas.doctype.subdomain_denylist.subdomain_denylist import seed_denylist
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.tests._mocks import fake_task

ROOT_DOMAIN = "blr1.frappe.dev"
REGION = "blr1"


def _ensure_root_domain() -> None:
	"""A single active Root Domain `blr1.frappe.dev` (region blr1) — the active region
	the endpoints resolve against. Mirrors test_api_signup."""
	if not frappe.db.exists("Domain Provider", "route53-routing-test"):
		frappe.get_doc(
			{
				"doctype": "Domain Provider",
				"provider_name": "route53-routing-test",
				"provider_type": "Route53",
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("TLS Provider", "letsencrypt-routing-test"):
		frappe.get_doc(
			{
				"doctype": "TLS Provider",
				"provider_name": "letsencrypt-routing-test",
				"provider_type": "Let's Encrypt",
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Root Domain", ROOT_DOMAIN):
		frappe.get_doc(
			{
				"doctype": "Root Domain",
				"domain": ROOT_DOMAIN,
				"region": REGION,
				"is_active": 1,
				"domain_provider": "route53-routing-test",
				"tls_provider": "letsencrypt-routing-test",
			}
		).insert(ignore_permissions=True)
	frappe.db.set_value("Root Domain", ROOT_DOMAIN, "is_active", 1)
	for name in frappe.get_all("Root Domain", filters={"is_active": 1}, pluck="name"):
		if name != ROOT_DOMAIN:
			frappe.db.set_value("Root Domain", name, "is_active", 0)


def _running_vm(**overrides):
	"""A Running, non-proxy bench VM with a public ipv6 — the shape caller resolution
	resolves and the endpoints write for."""
	vm = _new_vm(**overrides)
	vm.db_set("status", "Running")
	vm.reload()
	return vm


def _make_subdomain(label: str, vm: str, *, region: str = REGION, active: int = 1):
	return frappe.get_doc(
		{
			"doctype": "Subdomain",
			"subdomain": label,
			"virtual_machine": vm,
			"region": region,
			"active": active,
		}
	).insert(ignore_permissions=True)


def _purge() -> None:
	for name in frappe.get_all("Subdomain", pluck="name"):
		frappe.delete_doc("Subdomain", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Bench Routing Audit", pluck="name"):
		frappe.delete_doc("Bench Routing Audit", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Subdomain Denylist", pluck="name"):
		frappe.delete_doc("Subdomain Denylist", name, force=1, ignore_permissions=True)


class _RoutingTestCase(IntegrationTestCase):
	"""Base: a clean fleet + a single active Root Domain, and a helper to call an
	endpoint with `frappe.local.request_ip` set to a chosen source. The endpoints are
	@rate_limit-decorated (a request context the unit harness lacks), so we call the
	undecorated `.__wrapped__` implementation, like test_api_signup."""

	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_ensure_root_domain()
		_purge()
		self.addCleanup(self._clear_request_ip)

	def _clear_request_ip(self) -> None:
		frappe.local.request_ip = None

	def _as(self, source_ip: str, endpoint, **kwargs):
		"""Call an endpoint as a caller whose request source is `source_ip`."""
		frappe.local.request_ip = source_ip
		return endpoint.__wrapped__(**kwargs)


# ---------------------------------------------------------------------------
# Caller resolution
# ---------------------------------------------------------------------------


class TestCallerResolution(_RoutingTestCase):
	def test_register_resolves_vm_from_source_ip(self) -> None:
		vm = _running_vm()
		result = self._as(vm.ipv6_address, bench_routing.register, label="acme")
		self.assertEqual(result["status"], "ok")
		# The inserted row's VM is the SOURCE-resolved one, never a param.
		self.assertEqual(frappe.get_doc("Subdomain", "acme").virtual_machine, vm.name)

	def test_no_vm_uuid_parameter_is_honored(self) -> None:
		# A guest-supplied vm_uuid naming a DIFFERENT VM must be ignored — the caller is
		# always the source address. register takes only `label`; a stray body key is
		# dropped by the signature, so the resolved VM stays the source VM.
		caller = _running_vm()
		victim = _running_vm()
		frappe.local.request_ip = caller.ipv6_address
		# Pass the victim's name as a stray kwarg-shaped param via form_dict; the
		# undecorated signature only binds `label`, so `vm_uuid` cannot redirect it.
		result = bench_routing.register.__wrapped__(label="acme")
		self.assertEqual(frappe.get_doc("Subdomain", "acme").virtual_machine, caller.name)
		self.assertNotEqual(frappe.get_doc("Subdomain", "acme").virtual_machine, victim.name)
		self.assertEqual(result["status"], "ok")

	def test_unknown_source_is_a_clean_reject(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			self._as("2001:db8:dead::ffff", bench_routing.register, label="acme")
		self.assertEqual(frappe.db.count("Subdomain"), 0)

	def test_no_source_ip_is_a_clean_reject(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			self._as(None, bench_routing.register, label="acme")

	def test_terminated_vm_source_is_rejected(self) -> None:
		vm = _running_vm()
		vm.db_set("status", "Terminated")
		with self.assertRaises(frappe.ValidationError):
			self._as(vm.ipv6_address, bench_routing.register, label="acme")

	def test_proxy_vm_source_is_rejected(self) -> None:
		vm = _running_vm()
		vm.db_set("is_proxy", 1)
		with self.assertRaises(frappe.ValidationError):
			self._as(vm.ipv6_address, bench_routing.register, label="acme")

	def test_leftmost_xff_forgery_does_not_resolve_to_victim(self) -> None:
		# The one-way model's worst failure: a guest sends X-Forwarded-For: <victim/128>.
		# The unit boundary is "given this request_ip, the right VM or a reject." Under
		# the trusted-edge contract the worker reads the EDGE-supplied peer (the caller's
		# own /128), so request_ip is the caller, NOT the victim the guest named — the
		# write lands on the caller's VM, never the victim's.
		caller = _running_vm()
		victim = _running_vm()
		# The edge overwrote XFF to the real peer; request_ip is the caller, not victim.
		result = self._as(caller.ipv6_address, bench_routing.register, label="acme")
		self.assertEqual(result["status"], "ok")
		self.assertEqual(frappe.get_doc("Subdomain", "acme").virtual_machine, caller.name)
		# If request_ip were the FORGED victim value, resolution would target the victim;
		# assert the victim owns nothing.
		self.assertEqual(frappe.db.count("Subdomain", {"virtual_machine": victim.name}), 0)

	def test_terminated_row_does_not_shadow_the_live_owner_of_a_recycled_ip(self) -> None:
		# ipv6_address is not unique and allocate_ipv6 can recycle a Terminated VM's /128
		# onto a fresh Running VM. The resolver must filter Terminated in the QUERY so the
		# stale row never shadows the live owner — resolution lands on the Running VM.
		live = _running_vm()
		ip = live.ipv6_address
		dead = _new_vm()
		dead.db_set("ipv6_address", ip)  # the recycled /128 still on a Terminated row
		dead.db_set("status", "Terminated")
		result = self._as(ip, bench_routing.register, label="acme")
		self.assertEqual(result["status"], "ok")
		self.assertEqual(frappe.get_doc("Subdomain", "acme").virtual_machine, live.name)

	def test_ambiguous_duplicate_live_ip_fails_closed(self) -> None:
		# If two LIVE non-proxy VMs somehow share a /128, resolve neither (a write under
		# either would be wrong) — fail closed, not "arbitrary first row".
		a = _running_vm()
		b = _running_vm()
		b.db_set("ipv6_address", a.ipv6_address)
		with self.assertRaises(frappe.ValidationError):
			self._as(a.ipv6_address, bench_routing.register, label="acme")
		self.assertEqual(frappe.db.count("Subdomain"), 0)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


class TestRegister(_RoutingTestCase):
	def test_ok_inserts_active_row(self) -> None:
		vm = _running_vm()
		result = self._as(vm.ipv6_address, bench_routing.register, label="acme")
		self.assertEqual(result["status"], "ok")
		self.assertEqual(result["suffix"], ROOT_DOMAIN)
		row = frappe.get_doc("Subdomain", "acme")
		self.assertEqual(row.virtual_machine, vm.name)
		self.assertEqual(row.region, REGION)
		self.assertTrue(row.active)

	def test_taken_on_label_owned_by_another_vm(self) -> None:
		owner = _running_vm()
		_make_subdomain("acme", owner.name)
		other = _running_vm()
		result = self._as(other.ipv6_address, bench_routing.register, label="acme")
		self.assertEqual(result["status"], "taken")
		# The original owner's row is untouched; no second row inserted.
		self.assertEqual(frappe.db.count("Subdomain", {"subdomain": "acme"}), 1)
		self.assertEqual(frappe.get_doc("Subdomain", "acme").virtual_machine, owner.name)

	def test_taken_on_duplicate_entry_race(self) -> None:
		# Two benches race the same FREE label: both pass the pre-checks, the first wins
		# the unique key, the second's insert throws DuplicateEntryError → taken. Simulate
		# by forcing the insert to raise DuplicateEntryError (the row appears between the
		# pre-check and the insert).
		vm = _running_vm()
		real_get_doc = frappe.get_doc

		def racing_get_doc(*args, **kwargs):
			doc = real_get_doc(*args, **kwargs)
			if args and isinstance(args[0], dict) and args[0].get("doctype") == "Subdomain":

				def boom(*_a, **_k):
					raise frappe.DuplicateEntryError("Subdomain", "acme")

				doc.insert = boom
			return doc

		with patch("atlas.atlas.bench_routing.frappe.get_doc", side_effect=racing_get_doc):
			result = self._as(vm.ipv6_address, bench_routing.register, label="acme")
		self.assertEqual(result["status"], "taken")

	def test_taken_on_unique_validation_error_race(self) -> None:
		# `subdomain` is both the autoname source (PRIMARY) and `unique:1` (a secondary
		# unique index), so a losing race can surface as UniqueValidationError instead of
		# DuplicateEntryError. register must map BOTH to `taken`, not let it escape as a
		# 417 ValidationError.
		vm = _running_vm()
		real_get_doc = frappe.get_doc

		def racing_get_doc(*args, **kwargs):
			doc = real_get_doc(*args, **kwargs)
			if args and isinstance(args[0], dict) and args[0].get("doctype") == "Subdomain":

				def boom(*_a, **_k):
					raise frappe.UniqueValidationError("Subdomain", "acme", "subdomain")

				doc.insert = boom
			return doc

		with patch("atlas.atlas.bench_routing.frappe.get_doc", side_effect=racing_get_doc):
			result = self._as(vm.ipv6_address, bench_routing.register, label="acme")
		self.assertEqual(result["status"], "taken")

	def test_reserved_structural_label(self) -> None:
		vm = _running_vm()
		result = self._as(vm.ipv6_address, bench_routing.register, label="admin")
		self.assertEqual(result["status"], "reserved")
		self.assertEqual(frappe.db.count("Subdomain"), 0)

	def test_invalid_label(self) -> None:
		vm = _running_vm()
		result = self._as(vm.ipv6_address, bench_routing.register, label="ac.me")
		self.assertEqual(result["status"], "invalid")
		self.assertIn("single label", result["reason"])
		self.assertEqual(frappe.db.count("Subdomain"), 0)

	def test_idempotent_reregister_of_owned_label(self) -> None:
		# A retried register for the caller's OWN row is a clean ok (retry-after-transient).
		vm = _running_vm()
		self._as(vm.ipv6_address, bench_routing.register, label="acme")
		result = self._as(vm.ipv6_address, bench_routing.register, label="acme")
		self.assertEqual(result["status"], "ok")
		self.assertEqual(frappe.db.count("Subdomain", {"subdomain": "acme"}), 1)

	def test_at_limit_at_cap(self) -> None:
		# A 512 MB VM caps at 20 (the base tier). Fill it and assert the 21st is refused.
		vm = _running_vm()
		for i in range(20):
			_make_subdomain(f"s{i}", vm.name)
		result = self._as(vm.ipv6_address, bench_routing.register, label="overflow")
		self.assertEqual(result["status"], "at_limit")
		self.assertFalse(frappe.db.exists("Subdomain", "overflow"))
		# It NEVER evicts: the 20 already-routed stay routed.
		self.assertEqual(frappe.db.count("Subdomain", {"virtual_machine": vm.name}), 20)

	def test_register_inserts_proxy_reconcile_via_after_insert(self) -> None:
		# register's only proxy push is the row's own after_insert — no extra enqueue.
		vm = _running_vm()
		with patch("frappe.enqueue") as enqueue:
			self._as(vm.ipv6_address, bench_routing.register, label="acme")
		reconciles = [
			c
			for c in enqueue.call_args_list
			if c.args and c.args[0] == "atlas.atlas.doctype.subdomain.subdomain.auto_reconcile_region"
		]
		self.assertEqual(len(reconciles), 1)
		self.assertEqual(reconciles[0].kwargs["region"], REGION)


# ---------------------------------------------------------------------------
# deregister
# ---------------------------------------------------------------------------


class TestDeregister(_RoutingTestCase):
	def test_deletes_callers_own_row(self) -> None:
		vm = _running_vm()
		_make_subdomain("acme", vm.name)
		result = self._as(vm.ipv6_address, bench_routing.deregister, label="acme")
		self.assertEqual(result["status"], "ok")
		self.assertFalse(frappe.db.exists("Subdomain", "acme"))

	def test_another_vms_row_is_a_noop(self) -> None:
		owner = _running_vm()
		_make_subdomain("acme", owner.name)
		attacker = _running_vm()
		result = self._as(attacker.ipv6_address, bench_routing.deregister, label="acme")
		self.assertEqual(result["status"], "ok")
		# The owner's row is untouched — a guest can never deregister another VM's route.
		self.assertTrue(frappe.db.exists("Subdomain", "acme"))
		self.assertEqual(frappe.get_doc("Subdomain", "acme").virtual_machine, owner.name)

	def test_idempotent_on_absent_row(self) -> None:
		# A double drop / a rollback for a register that itself failed → clean ok.
		vm = _running_vm()
		result = self._as(vm.ipv6_address, bench_routing.deregister, label="never-existed")
		self.assertEqual(result["status"], "ok")

	def test_fires_proxy_reconcile_via_on_trash(self) -> None:
		vm = _running_vm()
		_make_subdomain("acme", vm.name)
		with patch("frappe.enqueue") as enqueue:
			self._as(vm.ipv6_address, bench_routing.deregister, label="acme")
		reconciles = [
			c
			for c in enqueue.call_args_list
			if c.args and c.args[0] == "atlas.atlas.doctype.subdomain.subdomain.auto_reconcile_region"
		]
		self.assertEqual(len(reconciles), 1)


# ---------------------------------------------------------------------------
# check_label
# ---------------------------------------------------------------------------


class TestCheckLabel(_RoutingTestCase):
	def _check(self, vm, label: str) -> dict:
		return self._as(vm.ipv6_address, bench_routing.check_label, label=label)

	def test_ok_for_a_free_label(self) -> None:
		vm = _running_vm()
		result = self._check(vm, "acme")
		self.assertEqual(result["status"], "ok")
		self.assertEqual(result["suffix"], ROOT_DOMAIN)

	def test_reserved_label(self) -> None:
		vm = _running_vm()
		result = self._check(vm, "admin")
		self.assertEqual(result["status"], "reserved")
		self.assertEqual(result["suffix"], ROOT_DOMAIN)

	def test_invalid_label_returns_typed_reason(self) -> None:
		vm = _running_vm()
		result = self._check(vm, "ac.me")
		self.assertEqual(result["status"], "invalid")
		self.assertIn("single label", result["reason"])

	def test_uppercase_is_invalid(self) -> None:
		vm = _running_vm()
		self.assertEqual(self._check(vm, "Acme")["status"], "invalid")

	def test_taken_by_a_live_subdomain(self) -> None:
		owner = _running_vm()
		_make_subdomain("acme", owner.name)
		caller = _running_vm()
		self.assertEqual(self._check(caller, "acme")["status"], "taken")

	def test_taken_by_a_live_site(self) -> None:
		frappe.get_doc({"doctype": "Site", "subdomain": "shop"}).insert(ignore_permissions=True)
		self.addCleanup(
			lambda: frappe.db.exists("Site", "shop.blr1.frappe.dev")
			and frappe.delete_doc("Site", "shop.blr1.frappe.dev", force=1, ignore_permissions=True)
		)
		vm = _running_vm()
		self.assertEqual(self._check(vm, "shop")["status"], "taken")

	def test_at_limit_mirrored(self) -> None:
		vm = _running_vm()
		for i in range(20):
			_make_subdomain(f"s{i}", vm.name)
		self.assertEqual(self._check(vm, "more")["status"], "at_limit")

	def test_check_label_writes_nothing(self) -> None:
		vm = _running_vm()
		self._check(vm, "acme")
		self.assertEqual(frappe.db.count("Subdomain"), 0)

	def test_non_resolving_source_throws(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			frappe.local.request_ip = "2001:db8:dead::1"
			bench_routing.check_label.__wrapped__(label="acme")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList(_RoutingTestCase):
	def test_returns_only_callers_rows_with_built_fqdn(self) -> None:
		vm = _running_vm()
		other = _running_vm()
		_make_subdomain("acme", vm.name)
		_make_subdomain("widgets", vm.name, active=0)
		_make_subdomain("notmine", other.name)
		result = self._as(vm.ipv6_address, bench_routing.list)
		labels = {d["label"]: d for d in result["domains"]}
		self.assertEqual(set(labels), {"acme", "widgets"})
		self.assertEqual(labels["acme"]["fqdn"], f"acme.{ROOT_DOMAIN}")
		self.assertTrue(labels["acme"]["active"])
		self.assertFalse(labels["widgets"]["active"])

	def test_empty_inventory_is_typed_not_a_throw(self) -> None:
		vm = _running_vm()
		result = self._as(vm.ipv6_address, bench_routing.list)
		self.assertEqual(result, {"domains": []})

	def test_non_resolving_source_throws_no_inventory(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			frappe.local.request_ip = "2001:db8:dead::2"
			bench_routing.list.__wrapped__()

	def test_terminated_source_throws(self) -> None:
		vm = _running_vm()
		_make_subdomain("acme", vm.name)
		vm.db_set("status", "Terminated")
		with self.assertRaises(frappe.ValidationError):
			self._as(vm.ipv6_address, bench_routing.list)

	def test_list_does_not_affect_cap(self) -> None:
		vm = _running_vm()
		for i in range(20):
			_make_subdomain(f"s{i}", vm.name)
		# At cap; list must not consume a slot, so a deregister-then-register still fits.
		self._as(vm.ipv6_address, bench_routing.list)
		self.assertEqual(frappe.db.count("Subdomain", {"virtual_machine": vm.name}), 20)


# ---------------------------------------------------------------------------
# Component G — the per-VM cap
# ---------------------------------------------------------------------------


class TestCap(_RoutingTestCase):
	def test_tier_lookup(self) -> None:
		def cap(mb):
			vm = frappe.get_doc({"doctype": "Virtual Machine"})
			vm.memory_megabytes = mb
			return bench_routing.cap_for_vm(vm)

		self.assertEqual(cap(512), 20)  # the base — every size in sizes.py today
		self.assertEqual(cap(8 * 1024), 20)
		self.assertEqual(cap(16 * 1024), 40)
		self.assertEqual(cap(32 * 1024), 80)
		self.assertEqual(cap(64 * 1024), 160)
		self.assertEqual(cap(128 * 1024), 160)

	def test_resize_reprices_the_cap(self) -> None:
		# A 16 GB VM caps at 40, not 20 — a resize re-prices for free.
		vm = _running_vm(memory_megabytes=16 * 1024)
		for i in range(20):
			_make_subdomain(f"s{i}", vm.name)
		# Still under the 40 cap after 20.
		result = self._as(vm.ipv6_address, bench_routing.register, label="twentyone")
		self.assertEqual(result["status"], "ok")


# ---------------------------------------------------------------------------
# Component H — the brand denylist
# ---------------------------------------------------------------------------


class TestDenylist(_RoutingTestCase):
	def _add(self, label: str, *, enabled: int = 1) -> None:
		frappe.get_doc(
			{"doctype": "Subdomain Denylist", "label": label, "reason": "test", "enabled": enabled}
		).insert(ignore_permissions=True)

	def test_register_rejects_an_enabled_denylist_row(self) -> None:
		self._add("paypal")
		vm = _running_vm()
		result = self._as(vm.ipv6_address, bench_routing.register, label="paypal")
		self.assertEqual(result["status"], "reserved")
		self.assertFalse(frappe.db.exists("Subdomain", "paypal"))

	def test_check_label_rejects_an_enabled_denylist_row(self) -> None:
		self._add("stripe")
		vm = _running_vm()
		self.assertEqual(
			self._as(vm.ipv6_address, bench_routing.check_label, label="stripe")["status"], "reserved"
		)

	def test_disabled_row_lifts_the_block_immediately(self) -> None:
		self._add("paypal", enabled=0)
		vm = _running_vm()
		# enabled=0 → not blocked, no migrate needed.
		self.assertEqual(self._as(vm.ipv6_address, bench_routing.register, label="paypal")["status"], "ok")

	def test_runtime_row_is_honored_on_next_call(self) -> None:
		vm = _running_vm()
		self.assertEqual(
			self._as(vm.ipv6_address, bench_routing.check_label, label="brandnew")["status"], "ok"
		)
		self._add("brandnew")
		self.assertEqual(
			self._as(vm.ipv6_address, bench_routing.check_label, label="brandnew")["status"], "reserved"
		)

	def test_seed_is_idempotent(self) -> None:
		first = seed_denylist()
		self.assertGreater(first, 0)
		# A re-run inserts nothing (every seed label already present).
		self.assertEqual(seed_denylist(), 0)

	def test_denylist_label_is_lowercased(self) -> None:
		frappe.get_doc({"doctype": "Subdomain Denylist", "label": "PayPal", "reason": "brand"}).insert(
			ignore_permissions=True
		)
		self.assertTrue(frappe.db.exists("Subdomain Denylist", "paypal"))


# ---------------------------------------------------------------------------
# Component I — the audit log
# ---------------------------------------------------------------------------


class TestAudit(_RoutingTestCase):
	def _audit_rows(self, **filters):
		return frappe.get_all(
			"Bench Routing Audit",
			filters=filters,
			fields=["endpoint", "label", "status", "business_reject", "vm", "source_ip"],
			order_by="creation asc",
		)

	def test_doctype_is_myisam(self) -> None:
		meta = frappe.get_meta("Bench Routing Audit")
		self.assertEqual(meta.engine, "MyISAM")

	def test_table_is_actually_myisam_at_migrate(self) -> None:
		# The whole rollback-survival argument rests on the table really being MyISAM —
		# verify the deployment's MariaDB didn't silently coerce it to InnoDB.
		engine = frappe.db.sql(
			"""SELECT ENGINE FROM information_schema.TABLES
			   WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'tabBench Routing Audit'""",
			frappe.conf.db_name,
		)
		self.assertTrue(engine, "Bench Routing Audit table not found")
		self.assertEqual(engine[0][0].upper(), "MYISAM")

	def test_register_ok_writes_one_row(self) -> None:
		vm = _running_vm()
		self._as(vm.ipv6_address, bench_routing.register, label="acme")
		rows = self._audit_rows(endpoint="register")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["status"], "ok")
		self.assertEqual(rows[0]["business_reject"], 0)
		self.assertEqual(rows[0]["vm"], vm.name)
		self.assertEqual(rows[0]["label"], "acme")
		self.assertEqual(rows[0]["source_ip"], vm.ipv6_address)

	def test_register_reject_writes_a_business_reject_row(self) -> None:
		owner = _running_vm()
		_make_subdomain("acme", owner.name)
		other = _running_vm()
		self._as(other.ipv6_address, bench_routing.register, label="acme")
		rows = self._audit_rows(endpoint="register", status="taken")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["business_reject"], 1)

	def test_reject_row_survives_a_request_rollback(self) -> None:
		# The InnoDB-would-lose-it regression: a rejected register that throws on the
		# resolution path still leaves its audit row. A non-resolving source throws
		# (rolling back the request transaction); the MyISAM row must persist.
		try:
			frappe.local.request_ip = "2001:db8:dead::99"
			bench_routing.register.__wrapped__(label="acme")
		except frappe.ValidationError:
			pass
		frappe.db.rollback()  # the request transaction unwinds
		rows = self._audit_rows(status="unresolved")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["vm"], "")  # no VM resolved
		self.assertEqual(rows[0]["source_ip"], "2001:db8:dead::99")  # the spoofer's /128
		self.assertEqual(rows[0]["business_reject"], 1)

	def test_unresolved_records_blank_vm_and_source_ip(self) -> None:
		try:
			self._as("2001:db8:dead::1", bench_routing.list)
		except frappe.ValidationError:
			pass
		rows = self._audit_rows(endpoint="list", status="unresolved")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["vm"], "")
		self.assertEqual(rows[0]["source_ip"], "2001:db8:dead::1")

	def test_check_label_and_list_are_audited(self) -> None:
		vm = _running_vm()
		self._as(vm.ipv6_address, bench_routing.check_label, label="acme")
		self._as(vm.ipv6_address, bench_routing.list)
		self.assertEqual(len(self._audit_rows(endpoint="check_label")), 1)
		listing = self._audit_rows(endpoint="list")
		self.assertEqual(len(listing), 1)
		self.assertEqual(listing[0]["label"], "")  # list carries no label

	def test_helper_does_not_commit(self) -> None:
		# The helper must NOT call frappe.db.commit() — an explicit commit would flush
		# partial transactional work before a reject's throw, defeating the rollback.
		vm = _running_vm()
		with patch("atlas.atlas.bench_routing.frappe.db.commit") as commit:
			self._as(vm.ipv6_address, bench_routing.register, label="acme")
		commit.assert_not_called()


# ---------------------------------------------------------------------------
# Component F — terminate (the only teardown) + no sweeper
# ---------------------------------------------------------------------------


class TestTeardown(_RoutingTestCase):
	def test_terminate_deletes_every_subdomain_for_the_vm(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _running_vm()
		_make_subdomain("acme", vm.name)
		_make_subdomain("widgets", vm.name)
		self.assertEqual(frappe.db.count("Subdomain", {"virtual_machine": vm.name}), 2)
		vm.db_set("status", "Stopped")
		vm.reload()
		with patch.object(module, "run_task", return_value=fake_task(name="task-term")):
			vm.terminate()
		self.assertEqual(frappe.db.count("Subdomain", {"virtual_machine": vm.name}), 0)

	def test_no_sweeper_in_the_scheduler(self) -> None:
		# Push-only has NO scheduled teardown: assert the scheduler carries no bench-
		# routing reconcile/sweep entry (the entry was removed when the model went
		# push-only).
		from atlas import hooks

		# Flatten every scheduled method path. A scheduler event maps to either a
		# list of method paths (e.g. "daily") or a cron sub-dict of
		# {schedule: [paths]} (e.g. "cron") — handle both so this guard keeps
		# inspecting real method paths if the shape grows.
		def _method_paths(events: dict) -> list:
			paths = []
			for value in events.values():
				if isinstance(value, dict):
					for jobs in value.values():
						paths.extend(jobs)
				else:
					paths.extend(value)
			return paths

		for entry in _method_paths(hooks.scheduler_events):
			self.assertNotIn("bench_routing", entry)
			self.assertNotIn("reconcile_bench", entry)
			self.assertNotIn("sweep_stale", entry)

	def test_module_carries_no_pull_or_sweep_verbs(self) -> None:
		# The push-only converge removed the pull + sweeper entirely — guard against a
		# re-introduction.
		for gone in (
			"reconcile_bench_sites",
			"reconcile_all_bench_sites",
			"sweep_stale_subdomains",
			"route_hint",
			"_list_guest_sites",
		):
			self.assertFalse(hasattr(bench_routing, gone), f"{gone} should be gone (push-only)")
