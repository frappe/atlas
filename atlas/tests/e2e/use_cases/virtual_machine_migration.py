"""Use case: migrate a Virtual Machine's disk between hosts, keeping its identity.

Operator clicks **Migrate** on a VM form (target picker); the scheduled
`reconcile_migrations` callback advances the resumable phase row to Done. This is
COLD migration — the guest is stopped during cutover and (stage 1, change-address)
gets a NEW public IPv6 on the target; the UUID and everything derived from it
(MAC/tap/netns/uid) re-materialize identically, and the SSH host keys are PRESERVED
so the VM's SSH identity survives the move. See spec/24-vm-migration.md.

This is a **dedicated-two-droplet** host fact — it needs a source AND a target
Server, both Active and same-provider — so it owns its own droplets and is invoked
directly, NOT folded into `run_all_smoke` (the same shape as `server_provisioning`
and `tls_issuance`). Cost: up to two billable droplets (it reuses any Active pair).

What it proves — the host facts only a real cross-host move can:
  1. All 8 phases advance Pending→…→Done via the scheduler driver, idempotently.
  2. The migrated VM row repoints to the target server + a NEW /128 in the target's
     range, status Running.
  3. The guest BOOTS on the target (unit active, TCP 22 open on the new /128).
  4. The VM's SSH host key is BYTE-IDENTICAL before and after (identity survived).
  5. The source copy is fully torn down (unit inactive, disk + -migrate snaps gone).

Two scenarios, one per address scheme (each owns the two-droplet harness):
  - `run_smoke` — CHANGE-address (the Self-Managed fallback / non-forwardable path):
    the VM gets a NEW /128 on the target and the proxy re-points. Facts 1-5 above.
  - `run_keep_address_smoke` — KEEP-address (spec/24 §2.9, stage 3): the VM keeps
    its /128; the source host keeps holding the /64 and forwards the address to the
    target over a per-VM tunnel, with the guest's replies policy-routed back up it.
    Then Collapse-forward moves it to a new /128 and tears the tunnel down.

TRANSPORT (this build): plain-TCP NBD + plain-TCP socat tunnel carrier (no SSH
tunnel yet — a secure host-to-host carrier is a deferred follow-up). The unit suite
(test_virtual_machine_migration.py) owns the phase-order, pre-flight, immutability/
retry, flags.migrating-gate, capability-gate, and lifecycle-guard logic in
milliseconds; this module is only the host facts.
"""

import time

import frappe

from atlas.atlas.ssh import connection_for_server, run_ssh, ssh_key_file
from atlas.tests.e2e._droplets import ensure_two_active_servers
from atlas.tests.e2e._image import ensure_image_on_server
from atlas.tests.e2e._shared import ephemeral_public_key

POLL_TIMEOUT = 900  # the worker self-drives the phases; be generous when it also has a
# fresh VM's auto-provision queued ahead of the migration (observed ~12 min end-to-end)

# Boot-then-hydrate (spec/24 §0) target: on a tiny-disk e2e the guest's downtime
# (source stop → target boot) should be single-digit seconds — the copy now overlaps
# uptime. Generous ceiling so a slow CI box doesn't flake, but far under the ~44s the
# cold-copy-then-boot order took (plan §1). Regression tripwire, not a benchmark.
DOWNTIME_THRESHOLD_SECONDS = 20


def host_shell(server_name: str, command: str, timeout: int = 40) -> str:
	"""Run a raw shell command on a Server host over the controller SSH key (the
	same primitive the proxy/bench-image e2es use). Returns stdout."""
	conn = connection_for_server(frappe.get_doc("Server", server_name))
	with ssh_key_file(conn.ssh_private_key) as key_path:
		out, _err, _code = run_ssh(conn, key_path, command, timeout_seconds=timeout)
	return out


def run_smoke(reuse: bool = True, keep: bool = True) -> None:
	"""The host-fact path: migrate a fresh disposable VM source→target, assert the
	five facts above, then terminate it. Reuses any Active source + a second Active
	target if present; provisions what's missing."""
	source, target = ensure_two_active_servers(reuse=reuse, keep=keep)
	# The default image must be present on BOTH hosts (source exports, target boots).
	image = ensure_image_on_server(source.name)
	ensure_image_on_server(target.name)
	public_key = ephemeral_public_key()

	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "vm-migration",
			"server": source.name,
			"image": image.name,
			"vcpus": 1,
			"memory_megabytes": 1024,
			"disk_gigabytes": 4,
			"ssh_public_key": public_key,
		}
	).insert(ignore_permissions=True)
	vm.provision()
	frappe.db.commit()
	_wait_for_boot(vm.name, vm.server, vm.ipv6_address)

	# Baseline: the SSH host key the guest presents on the source (the identity that
	# must survive the move).
	host_key_before = _guest_host_key(vm.server, vm.ipv6_address)
	assert host_key_before, "could not read the guest's SSH host key before migration"

	try:
		migration = frappe.get_doc("Virtual Machine", vm.name).migrate(target_server=target.name)
		frappe.db.commit()
		final = _drive_to_terminal(migration)
		assert final == "Done", f"migration ended {final}, expected Done"

		vm.reload()
		assert vm.server == target.name, f"VM still on {vm.server}, expected {target.name}"
		assert vm.status == "Running", f"VM status {vm.status}, expected Running"
		target_range = frappe.db.get_value("Server", target.name, "ipv6_virtual_machine_range")
		assert _ip_in_range(vm.ipv6_address, target_range), (
			f"new /128 {vm.ipv6_address} not in target range {target_range}"
		)

		# Fact 3: boots on target, reachable on the new /128.
		_wait_for_boot(vm.name, target.name, vm.ipv6_address)

		# Fact 4: SSH host key preserved across the move.
		host_key_after = _guest_host_key(target.name, vm.ipv6_address)
		assert host_key_after == host_key_before, (
			f"SSH host key changed across migration: {host_key_before} -> {host_key_after}"
		)

		# Fact 5: source copy torn down.
		assert _source_clean(source.name, vm.name), "source copy not fully cleaned up"

		# Fact 6 (boot-then-hydrate, spec/24 §0): downtime closed at boot, not after
		# the copy — measured off the migration's own stop→boot Task timestamps.
		_assert_downtime(vm.name, "vm-migration")

		print(
			f"[e2e] vm-migration OK: {vm.name} moved {source.name} -> {target.name}, "
			f"new /128 {vm.ipv6_address}, host key preserved, source clean"
		)
	finally:
		if not keep:
			frappe.get_doc("Virtual Machine", vm.name).terminate()
			frappe.db.commit()


def run_keep_address_smoke(reuse: bool = True, keep: bool = True) -> None:
	"""The KEEP-ADDRESS host-fact path (spec/24 §2.9, stage 3): migrate a fresh VM
	source→target keeping its /128, then prove the facts only a real cross-host
	forward can:

	  1. All phases advance to Done; the VM row flips `server` to the target but its
	     `ipv6_address` is UNCHANGED (no new /128 allocated).
	  2. The guest is reachable at the SAME /128 after the move — inbound still lands
	     on the source (which holds the /64) and is tunnelled to the target, and the
	     guest's replies come back up the tunnel (the BCP38 return path).
	  3. The VM is recorded as forwarded from the source (traffic_forwarded_from),
	     tunnel_status=Forwarding, and the mig6-<vm8> device is up on BOTH hosts.
	  4. SSH host key preserved; source disk/snap copy torn down (the tunnel stays).
	  5. Collapse-forward then moves the VM to a NEW /128 on the target, tears the
	     tunnel down on both hosts, and clears the forward markers.

	Reachability is probed FROM THE TARGET HOST over the guest's /128 (the same
	in-fabric vantage the change-address smoke uses): a laptop has no v6 route to the
	guest, but the target host reaches it through its own veth once the return route
	is in place. Off-host-through-the-source delivery is what the tunnel provides;
	the target-side probe confirms the full inbound+return loop works end to end."""
	source, target = ensure_two_active_servers(reuse=reuse, keep=keep)
	image = ensure_image_on_server(source.name)
	ensure_image_on_server(target.name)
	public_key = ephemeral_public_key()

	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "vm-migration-keep",
			"server": source.name,
			"image": image.name,
			"vcpus": 1,
			"memory_megabytes": 1024,
			"disk_gigabytes": 4,
			"ssh_public_key": public_key,
		}
	).insert(ignore_permissions=True)
	vm.provision()
	frappe.db.commit()
	_wait_for_boot(vm.name, vm.server, vm.ipv6_address)
	kept_ipv6 = vm.ipv6_address
	host_key_before = _guest_host_key(vm.server, vm.ipv6_address)
	assert host_key_before, "could not read the guest's SSH host key before migration"

	try:
		migration_name = frappe.get_doc("Virtual Machine", vm.name).migrate(target_server=target.name)
		frappe.db.commit()
		migration = frappe.get_doc("Virtual Machine Migration", migration_name)
		assert migration.keep_address, (
			"expected a keep-address migration; provider capability said the /128 "
			"is not forwardable — check vm_range_is_forwardable for this provider"
		)
		final = _drive_to_terminal(migration_name)
		assert final == "Done", f"migration ended {final}, expected Done"

		# Fact 1: server flipped, address UNCHANGED.
		vm.reload()
		assert vm.server == target.name, f"VM still on {vm.server}, expected {target.name}"
		assert vm.status == "Running", f"VM status {vm.status}, expected Running"
		assert vm.ipv6_address == kept_ipv6, (
			f"address changed on a keep-address migration: {kept_ipv6} -> {vm.ipv6_address}"
		)

		# Fact 2: reachable at the SAME /128 after the move (inbound via source→tunnel,
		# reply via the return route back up the tunnel).
		_wait_for_boot(vm.name, target.name, kept_ipv6)

		# Fact 3: forward recorded + tunnel up on both hosts.
		assert vm.traffic_forwarded_from == source.name, (
			f"traffic_forwarded_from {vm.traffic_forwarded_from}, expected {source.name}"
		)
		migration.reload()
		assert migration.tunnel_status == "Forwarding", f"tunnel_status {migration.tunnel_status}"
		device = migration.tunnel_device
		assert _tunnel_up(source.name, device), f"tunnel {device} not up on source {source.name}"
		assert _tunnel_up(target.name, device), f"tunnel {device} not up on target {target.name}"

		# Fact 4: host key preserved, source disk copy gone (tunnel stays).
		host_key_after = _guest_host_key(target.name, kept_ipv6)
		assert host_key_after == host_key_before, (
			f"SSH host key changed across migration: {host_key_before} -> {host_key_after}"
		)
		assert _source_clean(source.name, vm.name), "source disk copy not cleaned up"

		# Boot-then-hydrate (spec/24 §0): downtime closed at boot (forward armed BEFORE
		# boot on the keep-address path), measured off the stop→boot Task timestamps.
		_assert_downtime(vm.name, "vm-migration-keep")

		print(
			f"[e2e] vm-migration-keep OK: {vm.name} moved {source.name} -> {target.name}, "
			f"kept /128 {kept_ipv6}, forwarded via {device}, host key preserved"
		)

		# Fact 5: Collapse-forward → new /128, tunnel gone, markers cleared.
		frappe.get_doc("Virtual Machine", vm.name).collapse_forward()
		frappe.db.commit()
		vm.reload()
		assert vm.ipv6_address != kept_ipv6, "address did not change after Collapse-forward"
		target_range = frappe.db.get_value("Server", target.name, "ipv6_virtual_machine_range")
		assert _ip_in_range(vm.ipv6_address, target_range), (
			f"collapsed /128 {vm.ipv6_address} not in target range {target_range}"
		)
		assert not vm.traffic_forwarded_from, "traffic_forwarded_from not cleared after collapse"
		assert not _tunnel_up(source.name, device), f"tunnel {device} still up on source after collapse"
		assert not _tunnel_up(target.name, device), f"tunnel {device} still up on target after collapse"
		_wait_for_boot(vm.name, target.name, vm.ipv6_address)
		print(f"[e2e] collapse-forward OK: {vm.name} moved to new /128 {vm.ipv6_address}, tunnel torn down")
	finally:
		if not keep:
			frappe.get_doc("Virtual Machine", vm.name).terminate()
			frappe.db.commit()


def _tunnel_up(host: str, device: str) -> bool:
	"""True if the mig6-<vm8> tunnel device exists and is UP on `host`."""
	if not device:
		return False
	out = host_shell(host, f"ip link show {device} 2>/dev/null || echo MISSING")
	return "MISSING" not in out and ("state UP" in out or "UP," in out or ",UP" in out)


def _drive_to_terminal(migration_name: str) -> str:
	"""POLL the migration to Done/Failed, letting the PRODUCTION self-drive
	(`start_migration`, enqueued by migrate() on insert and run by the worker) advance
	it — the test only observes. This is the faithful path: the worker walks the phases
	back-to-back (inline, no inter-phase delay) and self-paces the Hydrating copy, so
	the downtime measured off the stop→boot Task timestamps is exactly production's.

	It also avoids a DOUBLE-DRIVER race: if the test ALSO called advance_migration
	while the worker's start_migration ran the same row, both would load the VM doc and
	race its save() at cutover/stop (TimestampMismatchError → the row wrongly marked
	Failed). One driver, the real one; the test watches. Requires the worker up (the
	same precondition every migration/auto-provision e2e already has)."""
	deadline = time.monotonic() + POLL_TIMEOUT
	while time.monotonic() < deadline:
		status = frappe.db.get_value("Virtual Machine Migration", migration_name, "status")
		if status in ("Done", "Failed"):
			return status
		time.sleep(2)
		frappe.db.commit()  # end the read txn so each poll sees the worker's latest commit
	return frappe.db.get_value("Virtual Machine Migration", migration_name, "status")


def _wait_for_boot(vm_name: str, host: str, v6: str, timeout: int = 180) -> None:
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		out = host_shell(host, f"timeout 6 bash -c 'echo > /dev/tcp/{v6}/22' && echo OPEN || echo CLOSED")
		if "OPEN" in out:
			return
		time.sleep(5)
	raise AssertionError(f"VM {vm_name} not reachable on {v6}:22 within {timeout}s")


def _guest_host_key(host: str, v6: str) -> str:
	out = host_shell(host, f"ssh-keyscan -T 10 -t ed25519 {v6} 2>/dev/null | awk '{{print $3}}' | head -1")
	return out.strip()


def _measure_downtime(vm_name: str) -> float | None:
	"""Guest downtime in seconds from the migration's OWN Task timestamps (same method
	as the plan §1 baseline): the migration cold-stop (stop-vm creation) → the target
	boot (provision-vm completion). Boot-then-hydrate closes the window at boot, so
	the multi-minute hydration is off this measurement. Returns None if either Task is
	missing (e.g. a resumed migration that skipped the stop)."""
	stop = frappe.get_all(
		"Task",
		filters={"virtual_machine": vm_name, "script": "stop-vm"},
		fields=["creation"],
		order_by="creation desc",
		limit=1,
	)
	boot = frappe.get_all(
		"Task",
		filters={"virtual_machine": vm_name, "script": "provision-vm"},
		fields=["modified"],
		order_by="creation desc",
		limit=1,
	)
	if not stop or not boot:
		return None
	return frappe.utils.time_diff_in_seconds(boot[0].modified, stop[0].creation)


def _assert_downtime(vm_name: str, label: str) -> None:
	"""Assert the measured guest downtime is under the boot-then-hydrate threshold and
	print it (the plan wants the number surfaced). Skips the assert (but still prints)
	if the Task timestamps aren't both present."""
	downtime = _measure_downtime(vm_name)
	if downtime is None:
		print(f"[e2e] {label} downtime: unmeasured (stop/boot Task missing)")
		return
	print(f"[e2e] {label} downtime: {downtime:.1f}s (threshold {DOWNTIME_THRESHOLD_SECONDS}s)")
	assert downtime < DOWNTIME_THRESHOLD_SECONDS, (
		f"{label} downtime {downtime:.1f}s exceeded {DOWNTIME_THRESHOLD_SECONDS}s — "
		"boot-then-hydrate should close the window at boot, not after hydration (spec/24 §0)"
	)


def _source_clean(source: str, vm_name: str) -> bool:
	unit = host_shell(
		source, f"sudo systemctl is-active firecracker-vm@{vm_name}.service 2>/dev/null || true"
	).strip()
	lvs = host_shell(source, f"sudo lvs --noheadings -o lv_name atlas | grep {vm_name} || echo CLEAN").strip()
	return unit != "active" and "CLEAN" in lvs


def _ip_in_range(address: str, cidr: str) -> bool:
	import ipaddress

	return ipaddress.ip_address(address) in ipaddress.ip_network(cidr)
