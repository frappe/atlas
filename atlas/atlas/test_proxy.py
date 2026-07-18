import contextlib
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import proxy
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)


def _purge() -> None:
	# Tasks are deliberately NOT purged: they're append-only audit rows, every
	# assertion filters by the per-test VM name (a fresh UUID), so stale Tasks
	# can never match — and deleting them takes a FOR UPDATE NOWAIT lock that
	# flakes under the full-suite's transaction contention.
	for name in frappe.get_all("Custom Domain", pluck="name"):
		frappe.delete_doc("Custom Domain", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Subdomain", pluck="name"):
		frappe.delete_doc("Subdomain", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


def _make_subdomain(subdomain: str, vm: str, **overrides):
	doc = {"doctype": "Subdomain", "subdomain": subdomain, "virtual_machine": vm}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def _make_custom_domain(domain: str, vm: str, *, status: str = "Active", **overrides):
	doc = {"doctype": "Custom Domain", "domain": domain, "virtual_machine": vm, "status": status}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


# Each proxy reconcile now reads THREE maps in order — the wildcard subdomain `sites`
# map (http /map), the custom-domain :443 SNI map (stream-admin GET-SNI), and the
# custom-domain :80 ACME map (http /acme) — and syncs each only on drift. A test with
# no custom domains leaves the SNI + ACME maps empty (`{}\n`), so those two reads find
# no drift and add no write. EMPTY_MAP is the canonical empty body both serve.
EMPTY_MAP = "{}\n"


def _proxy_vm():
	"""A VM marked as a proxy — the reconcile target."""
	return _new_vm(is_proxy=1)


@contextlib.contextmanager
def _mock_ssh(responses):
	"""Patch the guest-SSH plumbing proxy.py uses. `responses` is a list of
	(stdout, stderr, exit_code) tuples returned by successive run_ssh calls.
	Yields the run_ssh MagicMock so a test can assert the commands/stdin sent."""
	run_ssh = MagicMock(side_effect=list(responses))
	key_cm = MagicMock()
	key_cm.__enter__ = MagicMock(return_value="/tmp/fake.key")
	key_cm.__exit__ = MagicMock(return_value=False)
	with (
		patch.object(proxy, "run_ssh", run_ssh),
		patch.object(proxy, "ssh_key_file", return_value=key_cm),
		patch.object(proxy, "connection_for_guest", return_value=MagicMock(ssh_private_key="KEY")),
	):
		yield run_ssh


class TestCanonicalJson(IntegrationTestCase):
	def test_matches_lua_persist_format(self) -> None:
		# Byte-identical to persist.lua / the compose harness expectation: sorted
		# keys, 2-space indent, one per line, trailing newline.
		out = proxy.canonical_json({"b": "2400::b", "a": "2400::a"})
		self.assertEqual(out, '{\n  "a": "2400::a",\n  "b": "2400::b"\n}\n')

	def test_empty_map_is_brace_brace_newline(self) -> None:
		self.assertEqual(proxy.canonical_json({}), "{}\n")


class TestReconcile(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()
		# reconcile records the proxy-sync Task with {"region": atlas_region()},
		# which reads Atlas Settings.region (no per-VM region field anymore). Pin
		# it so atlas_region() doesn't throw "Set Atlas Settings.region".
		frappe.db.set_single_value("Atlas Settings", "region", "blr1")

	def test_no_drift_skips_sync(self) -> None:
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		_make_subdomain("acme", site_vm.name)
		desired = proxy.canonical_json({"acme": site_vm.ipv6_address})
		# All three live maps already equal desired (sites=desired, sni+acme empty) → no
		# POST. Three reads (GET /map, GET-SNI, GET /acme), zero writes.
		with _mock_ssh([(desired, "", 0), (EMPTY_MAP, "", 0), (EMPTY_MAP, "", 0)]) as run_ssh:
			synced = proxy.reconcile_proxy(proxy_vm.name)
		self.assertFalse(synced)
		self.assertEqual(run_ssh.call_count, 3)  # the three GETs only

	def test_drift_triggers_bulk_sync_with_canonical_body(self) -> None:
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		_make_subdomain("acme", site_vm.name)
		desired = proxy.canonical_json({"acme": site_vm.ipv6_address})
		# The sites map drifts (live empty) → POST /sync; sni + acme are empty + in sync.
		# Reads: GET /map(empty), then /sync; GET-SNI(empty); GET /acme(empty).
		with _mock_ssh(
			[("{}\n", "", 0), ('{"synced":true}', "", 0), (EMPTY_MAP, "", 0), (EMPTY_MAP, "", 0)]
		) as run_ssh:
			synced = proxy.reconcile_proxy(proxy_vm.name)
		self.assertTrue(synced)
		self.assertEqual(run_ssh.call_count, 4)
		# The second call is the sites /sync; its stdin body is the canonical desired map.
		_, sync_kwargs = run_ssh.call_args_list[1]
		self.assertEqual(sync_kwargs["stdin"], desired)
		sync_command = run_ssh.call_args_list[1].args[2]
		self.assertIn("/sync", sync_command)
		self.assertIn("--data-binary", sync_command)

	def test_sni_map_drift_syncs_via_stream_admin(self) -> None:
		# An active custom domain populates the :443 SNI map (and the :80 ACME map); the live
		# SNI map is empty → SYNC-SNI over the stream-admin line protocol with the canonical body.
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		_make_custom_domain("shop.acme.com", site_vm.name, status="Active")
		sni_desired = proxy.canonical_json({"shop.acme.com": f"[{site_vm.ipv6_address}]:443"})
		acme_desired = proxy.canonical_json({"shop.acme.com": f"[{site_vm.ipv6_address}]"})
		# sites empty+in-sync; SNI drifts (empty live) → SYNC-SNI; ACME drifts → /acme/sync.
		with _mock_ssh(
			[(EMPTY_MAP, "", 0), (EMPTY_MAP, "", 0), ("ok\n", "", 0), (EMPTY_MAP, "", 0), ("{}", "", 0)]
		) as run_ssh:
			synced = proxy.reconcile_proxy(proxy_vm.name)
		self.assertTrue(synced)
		# The SNI sync command is the stream-admin SYNC-SNI verb with the canonical SNI body.
		sni_calls = [c for c in run_ssh.call_args_list if "SYNC-SNI" in c.args[2]]
		self.assertEqual(len(sni_calls), 1)
		self.assertEqual(sni_calls[0].kwargs["stdin"], sni_desired)
		# The ACME sync carries the bare-bracketed-v6 body to /acme/sync.
		acme_calls = [c for c in run_ssh.call_args_list if "/acme/sync" in c.args[2]]
		self.assertEqual(len(acme_calls), 1)
		self.assertEqual(acme_calls[0].kwargs["stdin"], acme_desired)

	def test_inactive_custom_domain_in_neither_map(self) -> None:
		# An INACTIVE custom domain (active=0) is in neither map — all three desired maps are
		# empty, the live maps are empty, so nothing drifts: no SNI sync and no ACME sync fire.
		# (There is no readiness gate: an active row is always in both maps; `active` is the
		# only switch.)
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		_make_custom_domain("shop.acme.com", site_vm.name, active=0)
		with _mock_ssh([(EMPTY_MAP, "", 0), (EMPTY_MAP, "", 0), (EMPTY_MAP, "", 0)]) as run_ssh:
			synced = proxy.reconcile_proxy(proxy_vm.name)
		self.assertFalse(synced)
		self.assertEqual([c for c in run_ssh.call_args_list if "SYNC-SNI" in c.args[2]], [])
		self.assertEqual([c for c in run_ssh.call_args_list if "/acme/sync" in c.args[2]], [])

	def test_drift_records_a_task_row(self) -> None:
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		_make_subdomain("acme", site_vm.name)
		with _mock_ssh([("{}\n", "", 0), ("ok", "", 0), (EMPTY_MAP, "", 0), (EMPTY_MAP, "", 0)]):
			proxy.reconcile_proxy(proxy_vm.name)
		tasks = frappe.get_all(
			"Task", filters={"virtual_machine": proxy_vm.name, "script": "proxy-sync"}, pluck="status"
		)
		self.assertEqual(tasks, ["Success"])

	def test_sync_failure_raises_and_records_failure(self) -> None:
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		_make_subdomain("acme", site_vm.name)
		with _mock_ssh([("{}\n", "", 0), ("", "boom", 1)]):
			with self.assertRaises(frappe.ValidationError):
				proxy.reconcile_proxy(proxy_vm.name)
		status = frappe.get_all(
			"Task", filters={"virtual_machine": proxy_vm.name, "script": "proxy-sync"}, pluck="status"
		)
		self.assertEqual(status, ["Failure"])

	def test_reconcile_proxies_targets_every_proxy_vm(self) -> None:
		# The fleet is global now: every is_proxy VM is a reconcile target.
		proxy_a = _proxy_vm()
		proxy_b = _proxy_vm()
		site_vm = _new_vm()
		_make_subdomain("acme", site_vm.name)
		# Each proxy: sites drifts (empty→sync), sni+acme empty (in sync). 4 calls each.
		with _mock_ssh([("{}\n", "", 0), ("ok", "", 0), (EMPTY_MAP, "", 0), (EMPTY_MAP, "", 0)] * 2):
			synced = proxy.reconcile_proxies()
		self.assertEqual(set(synced), {proxy_a.name, proxy_b.name})

	def test_reconcile_isolates_one_unreachable_proxy(self) -> None:
		proxy_a = _proxy_vm()
		proxy_b = _proxy_vm()
		site_vm = _new_vm()
		_make_subdomain("acme", site_vm.name)
		# First proxy's GET raises (guest wedged); the loop must still reach the
		# second and sync it. Order isn't guaranteed, so make BOTH paths viable:
		# one raises on GET, one syncs cleanly.
		desired = proxy.canonical_json({"acme": site_vm.ipv6_address})

		def ssh_side_effect(connection, key_path, command, timeout_seconds, stdin=None):
			# Identify the proxy by the mocked connection's host marker.
			if connection.host == "DEAD":
				raise RuntimeError("guest unreachable")
			# Any read (GET /map, GET-SNI, GET /acme) returns an empty live map → drift;
			# any write (/sync, SYNC-SNI, /acme/sync) succeeds.
			if "GET" in command:
				return ("{}\n", "", 0)
			return ("ok", "", 0)

		def conn_for(vm):
			host = "DEAD" if vm.name == proxy_a.name else "OK"
			return MagicMock(host=host, ssh_private_key="KEY")

		key_cm = MagicMock()
		key_cm.__enter__ = MagicMock(return_value="/tmp/fake.key")
		key_cm.__exit__ = MagicMock(return_value=False)
		with (
			patch.object(proxy, "run_ssh", MagicMock(side_effect=ssh_side_effect)),
			patch.object(proxy, "ssh_key_file", return_value=key_cm),
			patch.object(proxy, "connection_for_guest", side_effect=conn_for),
		):
			synced = proxy.reconcile_proxies()
		# The dead one is skipped; the healthy one still synced.
		self.assertEqual(synced, [proxy_b.name])
		_ = desired


def _reserved_ip(ip_address: str, server: str, vm: str | None = None):
	"""A Reserved IP row attached (in the DB) to `vm`, bypassing the vendor assign
	+ host NAT Task that real `attach()` runs — wildcard_targets only reads the
	row's ip_address/virtual_machine."""
	doc = frappe.get_doc(
		{
			"doctype": "Reserved IP",
			"ip_address": ip_address,
			"server": server,
			"provider_resource_id": f"do-{ip_address}",
			"virtual_machine": vm,
		}
	)
	return doc.insert(ignore_permissions=True)


class TestReadLiveMaps(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()
		frappe.db.set_single_value("Atlas Settings", "region", "blr1")

	def test_reads_all_three_maps_and_flags_in_sync(self) -> None:
		# The live maps equal desired (sites has the one subdomain, sni+acme empty) → every
		# map reports in_sync, parsed live == parsed desired. Three reads, zero writes.
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		_make_subdomain("acme", site_vm.name)
		sites_live = proxy.canonical_json({"acme": site_vm.ipv6_address})
		with _mock_ssh([(sites_live, "", 0), (EMPTY_MAP, "", 0), (EMPTY_MAP, "", 0)]) as run_ssh:
			maps = proxy.read_live_maps(proxy_vm.name)
		self.assertEqual(run_ssh.call_count, 3)  # read-only: three GETs, no writes
		self.assertEqual([c.args[2] for c in run_ssh.call_args_list][1], "stream-admin GET-SNI")
		self.assertEqual(maps["sites"]["live"], {"acme": site_vm.ipv6_address})
		self.assertTrue(maps["sites"]["in_sync"])
		self.assertTrue(maps["sni"]["in_sync"])
		self.assertTrue(maps["acme"]["in_sync"])

	def test_flags_drift_when_live_differs(self) -> None:
		# The live sites map is empty but desired has the subdomain → in_sync False, and the
		# returned live/desired differ so the Desk table can highlight the missing key.
		proxy_vm = _proxy_vm()
		site_vm = _new_vm()
		_make_subdomain("acme", site_vm.name)
		with _mock_ssh([(EMPTY_MAP, "", 0), (EMPTY_MAP, "", 0), (EMPTY_MAP, "", 0)]):
			maps = proxy.read_live_maps(proxy_vm.name)
		self.assertFalse(maps["sites"]["in_sync"])
		self.assertEqual(maps["sites"]["live"], {})
		self.assertEqual(maps["sites"]["desired"], {"acme": site_vm.ipv6_address})

	def test_read_failure_raises(self) -> None:
		# A wedged guest (non-zero read exit) must raise, not silently report empty maps —
		# a button that lied about what the proxy serves would be worse than no button.
		proxy_vm = _proxy_vm()
		with _mock_ssh([("", "boom", 1)]):
			with self.assertRaises(frappe.ValidationError):
				proxy.read_live_maps(proxy_vm.name)


class TestWildcardTargets(IntegrationTestCase):
	# The proxy fleet is global now (no per-region scoping), so wildcard_targets()
	# sees every is_proxy VM. Purge the whole fleet — and any Reserved IPs attached
	# to those VMs — so each test starts from an empty fleet and asserts against the
	# proxies it creates.
	def setUp(self) -> None:
		for name in frappe.get_all("Subdomain", pluck="name"):
			frappe.delete_doc("Subdomain", name, force=1, ignore_permissions=True)
		for vm in frappe.get_all("Virtual Machine", pluck="name"):
			for rip in frappe.get_all("Reserved IP", filters={"virtual_machine": vm}, pluck="name"):
				# on_trash refuses to delete an ATTACHED Reserved IP; clear the link
				# directly (no vendor detach needed — these are DB-only test rows).
				frappe.db.set_value("Reserved IP", rip, "virtual_machine", None)
				frappe.delete_doc("Reserved IP", rip, force=1, ignore_permissions=True)
			frappe.delete_doc("Virtual Machine", vm, force=1, ignore_permissions=True)

	def test_gathers_aaaa_from_proxy_v6_and_a_from_attached_reserved_ip(self) -> None:
		server = _ensure_test_server()
		proxy_a = _proxy_vm()
		proxy_a.db_set("ipv6_address", "2400:6180::a")
		_reserved_ip("198.51.100.10", server, proxy_a.name)

		ipv4, ipv6 = proxy.wildcard_targets()
		self.assertEqual(ipv4, ["198.51.100.10"])
		self.assertEqual(ipv6, ["2400:6180::a"])

	def test_proxy_without_reserved_ip_contributes_only_aaaa(self) -> None:
		proxy_a = _proxy_vm()
		proxy_a.db_set("ipv6_address", "2400:6180::a")  # no Reserved IP attached
		ipv4, ipv6 = proxy.wildcard_targets()
		self.assertEqual(ipv4, [])
		self.assertEqual(ipv6, ["2400:6180::a"])

	def test_multiple_proxies_round_robin(self) -> None:
		server = _ensure_test_server()
		a = _proxy_vm()
		a.db_set("ipv6_address", "2400:6180::a")
		_reserved_ip("198.51.100.10", server, a.name)
		b = _proxy_vm()
		b.db_set("ipv6_address", "2400:6180::b")
		_reserved_ip("198.51.100.11", server, b.name)
		ipv4, ipv6 = proxy.wildcard_targets()
		self.assertEqual(sorted(ipv4), ["198.51.100.10", "198.51.100.11"])
		self.assertEqual(sorted(ipv6), ["2400:6180::a", "2400:6180::b"])

	def test_terminated_proxy_is_excluded(self) -> None:
		# A terminated proxy's guest is gone and its /128 no longer answers, so its
		# address must drop out of the wildcard — else half the round-robin blackholes.
		live = _proxy_vm()
		live.db_set("ipv6_address", "2400:6180::a")
		dead = _proxy_vm()
		dead.db_set("ipv6_address", "2400:6180::b")
		dead.db_set("status", "Terminated")
		ipv4, ipv6 = proxy.wildcard_targets()
		self.assertEqual(ipv4, [])
		self.assertEqual(ipv6, ["2400:6180::a"])


class TestPushCert(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()
		# push_cert scopes the cert dir under atlas_region() (Atlas Settings.region),
		# not a per-VM region field. Pin it to "blr1" so the path assertions below hold.
		frappe.db.set_single_value("Atlas Settings", "region", "blr1")

	def test_push_cert_writes_both_pems_and_reloads(self) -> None:
		proxy_vm = _proxy_vm()
		# 3 SSH calls: write fullchain, write privkey, reload.
		with _mock_ssh([("", "", 0), ("", "", 0), ("", "", 0)]) as run_ssh:
			proxy.push_cert(proxy_vm.name, fullchain="FULL", privkey="PRIV")
		self.assertEqual(run_ssh.call_count, 3)
		commands = [c.args[2] for c in run_ssh.call_args_list]
		stdins = [c.kwargs.get("stdin") for c in run_ssh.call_args_list]
		# Region-scoped cert dir, private key via stdin (never in argv), reload last.
		self.assertIn("fullchain.pem", commands[0])
		self.assertIn("blr1", commands[0])
		self.assertEqual(stdins[0], "FULL")
		self.assertIn("privkey.pem", commands[1])
		self.assertIn("0600", commands[1])
		self.assertEqual(stdins[1], "PRIV")
		# Last command repoints the flat cert symlink at this region's dir, then
		# reloads — so the pushed cert (region-scoped) is what nginx serves.
		self.assertIn("ln -sfn", commands[2])
		self.assertIn("blr1/fullchain.pem", commands[2])
		self.assertIn(f"{proxy.CERT_DIRECTORY}/fullchain.pem", commands[2])
		self.assertIn("nginx", commands[2])
		self.assertIn("reload", commands[2])

	def test_push_cert_records_task(self) -> None:
		proxy_vm = _proxy_vm()
		with _mock_ssh([("", "", 0), ("", "", 0), ("", "", 0)]):
			proxy.push_cert(proxy_vm.name, fullchain="FULL", privkey="PRIV")
		status = frappe.get_all(
			"Task", filters={"virtual_machine": proxy_vm.name, "script": "proxy-push-cert"}, pluck="status"
		)
		self.assertEqual(status, ["Success"])

	def test_push_cert_raises_on_reload_failure(self) -> None:
		proxy_vm = _proxy_vm()
		with _mock_ssh([("", "", 0), ("", "", 0), ("", "nginx: bad config", 1)]):
			with self.assertRaises(frappe.ValidationError):
				proxy.push_cert(proxy_vm.name, fullchain="FULL", privkey="PRIV")


class TestBuildProxy(IntegrationTestCase):
	"""build_proxy is now a thin wrapper over image_builder.run_build (handed the
	`proxy` recipe). The upload/build/finalize logic lives in image_builder +
	image_recipes; this suite covers what build_proxy itself still owns — the
	is_proxy guard — and re-asserts the end-to-end build through the seam.
	The recipe's tree enumeration + the finalize command are unit-covered in
	test_image_builder.py."""

	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()

	def test_non_proxy_vm_is_rejected(self) -> None:
		plain_vm = _new_vm()  # is_proxy unset
		with self.assertRaises(frappe.ValidationError):
			proxy.build_proxy(plain_vm.name)
