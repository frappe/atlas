"""Manual verification of spec/18 bench self-routing (the one-way push model) on an
existing bench VM.

Runs the host-bound checks the spec requires — all driven by the REAL in-guest
`bench-domain-provider` binary (the Phase-1 pilot plug-in, formerly `atlas-route`) over
IPv6, the only run that can prove the trust root (the controller resolves the calling VM
from the request's v6 source /128, no parameter):

  1. register reserves a name BEFORE new-site + the proxy serves it
  2. create-failure rollback (register, force new-site fail, deregister) leaves no stray
  3. drop + deregister drops the route from the proxy live map
  4. the host-level queries answer correctly (wildcard-domains = the region wildcard;
     proxy-servers includes this proxy's public IP) — the new Phase-1 verbs
  5. a direct VirtualMachine.terminate leaves no Subdomain (Component F, total)

NOTE on the verb contract (the move off `atlas-route`): the binary takes the FULL FQDN
(`<label>.<region domain>`), peels the region wildcard suffix to the bare label, and POSTs
the controller; `register` is now FAIL-CLOSED (a transport error → non-zero → pilot aborts
the create) and the old `check-label`/`list` verbs are gone (pilot drives deregister
itself on drop, so guest-side stray clearing has no equivalent).

against a REAL running bench VM and a REAL running proxy VM. Requires no TLS, no
reserved IP, no golden snapshot bake; just the two VMs and a working proxy.

Usage (on bootstrap.local, with a running bench VM and proxy VM):

    bench --site bootstrap.local execute \\
        atlas.tests.e2e.use_cases.bench_self_routing.run \\
        --kwargs '{"bench_vm": "<vm-name>", "proxy_vm": "<proxy-vm-name>"}'

    # checks 1-4 only (leave the VM running for re-runs):
    bench --site bootstrap.local execute \\
        atlas.tests.e2e.use_cases.bench_self_routing.run \\
        --kwargs '{"bench_vm": "<vm>", "proxy_vm": "<proxy>", "terminate": false}'

The bench VM must be:
  - Running, non-proxy, with a public ipv6_address
  - Have bench-cli installed with a bench named 'atlas' under
    /home/frappe/bench-cli/benches/atlas
  - Have /usr/local/bin/bench-domain-provider installed (build.sh) and
    /etc/atlas-routing.env pointing at this controller (cold inject / warm freshen)

The proxy VM must be:
  - Running, with the Atlas proxy (nginx+Lua) already built
  - Reachable over guest SSH by the controller

The run leaves transient Subdomain rows behind only if it fails before teardown; the
finally block clears every row this run could have created.
"""

import json

import frappe

from atlas.atlas import proxy
from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
from atlas.atlas.placement import active_root_domain
from atlas.atlas.ssh import connection_for_guest

# The label used for the guest-reserved test site. Short and clearly synthetic so a
# leaked row is obvious, distinct from the acme/shop labels other e2e use cases use.
_LABEL = "ws-e2e-test"
# A label kept only for defensive cleanup of rows older runs may have stranded.
_STRAY = "stray-e2e-test"
# A label reserved then force-failed (the create-failure rollback for check 2).
_ROLLBACK = "fail-e2e-test"

_BENCH = "/home/frappe/bench-cli/benches/atlas"

# The baked MariaDB root password (bench/bench.toml `root_password`) drop-site prompts
# for. Kept in step with bench.toml.
_BAKED_MARIADB_ROOT_PASSWORD = "mariadb-root"


def run(bench_vm: str, proxy_vm: str, terminate: bool = True) -> None:
	"""Run the spec/18 host-bound checks and print a pass/fail summary.

	`terminate=False` skips check 5 (the destructive one) so the VM survives for
	re-runs. Cleans up its own Subdomain rows in a finally block."""
	_preflight(bench_vm, proxy_vm)

	region = active_root_domain().region
	domain = active_root_domain().domain
	fqdn = f"{_LABEL}.{domain}"

	print(f"[bench-self-routing] bench_vm={bench_vm}  proxy_vm={proxy_vm}")
	print(f"[bench-self-routing] region={region}  domain={domain}  test_fqdn={fqdn}")

	site_v6 = frappe.db.get_value("Virtual Machine", bench_vm, "ipv6_address")

	try:
		_check_register_reserves_and_serves(bench_vm, proxy_vm, domain, fqdn, _LABEL, site_v6)
		_check_create_failure_rollback(bench_vm, domain)
		_check_drop_deregister_deconverges(bench_vm, proxy_vm, domain, fqdn, _LABEL, site_v6)
		_check_host_queries(bench_vm, proxy_vm, domain)
		if terminate:
			_check_terminate_cleanup(bench_vm, fqdn)
		else:
			print("\n[5] terminate cleanup SKIPPED (terminate=False)")
	finally:
		_cleanup(_LABEL, _STRAY, _ROLLBACK)

	print("")
	print("=" * 64)
	ran = 5 if terminate else 4
	print(f"bench self-routing (push-only): {ran} check(s) PASSED")
	print("=" * 64)


# ---------------------------------------------------------------------------
# Check 1: register reserves BEFORE create + the proxy serves it
# ---------------------------------------------------------------------------


def _check_register_reserves_and_serves(
	bench_vm: str, proxy_vm: str, domain: str, fqdn: str, label: str, site_v6: str
) -> None:
	"""`bench-domain-provider register <fqdn>` from inside the guest reserves the name (the
	row appears on register, not after create) — peeling the wildcard suffix to the bare
	label — resolves THIS VM by its v6 source /128, then the create + proxy reconcile serves
	it end to end."""
	print(f"\n[1] register reserves + serves: bench-domain-provider register {fqdn} inside the guest ...")

	assert not frappe.db.exists("Subdomain", label), f"a stale Subdomain '{label}' exists before register"
	_guest(bench_vm, f"bench-domain-provider register {fqdn}")
	row = frappe.get_doc("Subdomain", label)
	assert row.virtual_machine == bench_vm and row.active, (
		f"register did not reserve {label} for this VM (vm={row.virtual_machine}, active={row.active})"
	)
	# The trust root: caller resolution found THIS VM by its v6 source /128.
	audit = frappe.get_all(
		"Bench Routing Audit",
		filters={"endpoint": "register", "label": label, "status": "ok"},
		fields=["vm", "source_ip"],
		order_by="creation desc",
		limit=1,
	)
	assert audit and audit[0]["vm"] == bench_vm, f"register audit did not resolve this VM: {audit}"
	assert audit[0]["source_ip"] == site_v6, (
		f"register resolved source /128 {audit[0]['source_ip']} != this VM's v6 {site_v6}"
	)

	# Build the local site (the guest's own action), then reconcile the proxy and read
	# its live map back — the route is actually served, not just a DB row.
	_guest(
		bench_vm,
		(
			f'sudo -u frappe bash -lc "export PATH=/home/frappe/bench-cli:$PATH; cd {_BENCH}; '
			f'bench -b atlas new-site {fqdn} --admin-password atlas-baked --apps erpnext"'
		),
		timeout=600,
	)
	proxy.reconcile_proxy(proxy_vm)
	live = _read_live_map(proxy_vm)
	assert live.get(label) == site_v6, f"proxy live map does not serve {label} → {site_v6}: {live}"
	print(f"[1] PASS — guest-reserved {fqdn} resolved this VM by v6 source and is served by the proxy")


# ---------------------------------------------------------------------------
# Check 2: create-failure rollback leaves no stray
# ---------------------------------------------------------------------------


def _check_create_failure_rollback(bench_vm: str, domain: str) -> None:
	"""register a label, force `bench new-site` to FAIL, then deregister (the rollback)
	— assert no stale Subdomain survives (orphan-free, register-first)."""
	rollback_fqdn = f"{_ROLLBACK}.{domain}"
	print(f"\n[2] create-failure rollback: register {rollback_fqdn}, force new-site fail, deregister ...")

	_guest(bench_vm, f"bench-domain-provider register {rollback_fqdn}")
	assert frappe.db.exists("Subdomain", _ROLLBACK), "register did not reserve the rollback label"
	# A bogus app name makes new-site fail AFTER the reservation.
	_stdout, _stderr, code = _guest_raw(
		bench_vm,
		(
			f'sudo -u frappe bash -lc "export PATH=/home/frappe/bench-cli:$PATH; cd {_BENCH}; '
			f'bench -b atlas new-site {_ROLLBACK}.{domain} --admin-password atlas-baked --apps no_such_app_xyz"'
		),
		timeout=300,
	)
	assert code != 0, "the forced new-site failure unexpectedly succeeded"
	_guest(bench_vm, f"bench-domain-provider deregister {rollback_fqdn}")
	assert not frappe.db.exists("Subdomain", _ROLLBACK), "the create-failure rollback left a stale Subdomain"
	print("[2] PASS — register-then-fail-then-deregister left no stale route")


# ---------------------------------------------------------------------------
# Check 3: drop + deregister deconverges the proxy
# ---------------------------------------------------------------------------


def _check_drop_deregister_deconverges(
	bench_vm: str, proxy_vm: str, domain: str, fqdn: str, label: str, site_v6: str
) -> None:
	"""Drop the site from inside the guest, deregister, assert the route DROPS from the
	proxy's live map (deregister's on_trash deconverges)."""
	print(f"\n[3] drop + deregister: drop-site {fqdn} then bench-domain-provider deregister {fqdn} ...")

	_guest(
		bench_vm,
		(
			f'sudo -u frappe bash -lc "export PATH=/home/frappe/bench-cli:$PATH; cd {_BENCH}; '
			f'echo {_BAKED_MARIADB_ROOT_PASSWORD} | bench -b atlas drop-site {fqdn} --no-backup --force"'
		),
		timeout=300,
	)
	_guest(bench_vm, f"bench-domain-provider deregister {fqdn}")
	assert not frappe.db.exists("Subdomain", label), "deregister did not delete the route"

	proxy.reconcile_proxy(proxy_vm)
	live = _read_live_map(proxy_vm)
	assert label not in live, f"proxy live map still serves {label} after drop+deregister: {live}"
	print(f"[3] PASS — dropped+deregistered {fqdn} is gone from the proxy's live map")


# ---------------------------------------------------------------------------
# Check 4: the host-level queries answer correctly (the new Phase-1 verbs)
# ---------------------------------------------------------------------------


def _check_host_queries(bench_vm: str, proxy_vm: str, domain: str) -> None:
	"""`bench-domain-provider wildcard-domains` returns the region wildcard pilot
	constrains site names to, and `proxy-servers` returns this proxy's public IP — the
	edge pilot locks its nginx down to (closing the spec/18 trust-root gap). Both are
	host-level (no VM-identifying arg) and exit 0 with a JSON list on stdout.

	(This replaces the retired `list`/stray-clear check: the new contract has no `list`
	verb — pilot drives `deregister` itself on drop, so guest-side stray clearing has no
	equivalent.)"""
	print("\n[4] host-level queries: wildcard-domains + proxy-servers ...")

	wildcards = json.loads(_guest(bench_vm, "bench-domain-provider wildcard-domains").strip())
	assert wildcards == [f"*.{domain}"], f"wildcard-domains returned {wildcards!r}, expected ['*.{domain}']"

	ips = json.loads(_guest(bench_vm, "bench-domain-provider proxy-servers").strip())
	proxy_v6 = frappe.db.get_value("Virtual Machine", proxy_vm, "ipv6_address")
	assert proxy_v6 in ips, f"proxy-servers {ips!r} does not include this proxy's v6 {proxy_v6!r}"
	print(f"[4] PASS — wildcard-domains={wildcards} proxy-servers includes {proxy_v6}")


# ---------------------------------------------------------------------------
# Check 5: VirtualMachine.terminate() cleans up Subdomains (Component F)
# ---------------------------------------------------------------------------


def _check_terminate_cleanup(bench_vm: str, fqdn: str) -> None:
	"""Re-register a route, terminate the VM, assert no stale Subdomain rows remain.

	NOTE: this terminates the bench VM. Run only when decommissioning it after
	verification (pass terminate=False to skip)."""
	print("\n[5] terminate cleanup (VirtualMachine.terminate) ...")
	print(f"[5] WARNING: this will terminate {bench_vm}")

	_guest(bench_vm, f"bench-domain-provider register {fqdn}")
	assert frappe.db.count("Subdomain", {"virtual_machine": bench_vm}) > 0, (
		"expected the VM to own a Subdomain before terminate"
	)
	frappe.db.commit()
	frappe.get_doc("Virtual Machine", bench_vm).terminate()
	frappe.db.commit()
	remaining = frappe.db.count("Subdomain", {"virtual_machine": bench_vm})
	assert remaining == 0, f"terminate left {remaining} stale Subdomain row(s) for {bench_vm}"
	print(f"[5] PASS — VirtualMachine.terminate() deleted all Subdomains for {bench_vm}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _preflight(bench_vm: str, proxy_vm: str) -> None:
	vm = frappe.get_doc("Virtual Machine", bench_vm)
	assert vm.status == "Running", f"bench VM {bench_vm} is not Running (status={vm.status})"
	assert not vm.is_proxy, f"{bench_vm} is a proxy VM"
	assert vm.ipv6_address, f"{bench_vm} has no ipv6_address"

	pvm = frappe.get_doc("Virtual Machine", proxy_vm)
	assert pvm.status == "Running", f"proxy VM {proxy_vm} is not Running (status={pvm.status})"
	assert pvm.is_proxy, f"{proxy_vm} is not a proxy VM"

	# The guest must carry the provider binary + config, or every check is a no-op.
	stdout, _stderr, code = _guest_raw(
		bench_vm, "test -x /usr/local/bin/bench-domain-provider && cat /etc/atlas-routing.env"
	)
	assert code == 0, f"{bench_vm} is missing /usr/local/bin/bench-domain-provider or /etc/atlas-routing.env"
	assert "ATLAS_BASE_URL=" in stdout, f"{bench_vm} /etc/atlas-routing.env has no ATLAS_BASE_URL: {stdout!r}"


def _guest(vm_name: str, command: str, timeout: int = 120) -> str:
	stdout, stderr, code = _guest_raw(vm_name, command, timeout)
	assert code == 0, f"guest command failed (exit {code}): {command}\n{stderr[-500:]}"
	return stdout


def _guest_raw(vm_name: str, command: str, timeout: int = 120) -> tuple[str, str, int]:
	vm = frappe.get_doc("Virtual Machine", vm_name)
	connection = connection_for_guest(vm)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		result = run_ssh(connection, key_path, command, timeout_seconds=timeout)
	# The in-guest `atlas-route` POST commits the Subdomain write in a SEPARATE web
	# request/connection. This long-lived `bench execute` transaction snapshots under
	# MariaDB's REPEATABLE READ at its first read, so without refreshing it the assertions
	# can't SEE the guest's just-committed write. Roll back our (read-only) transaction so
	# the next read observes the controller's committed state.
	frappe.db.rollback()
	return result


def _read_live_map(proxy_vm_name: str) -> dict:
	"""SSH the proxy VM and read its live /map, returned as a plain dict."""
	vm = frappe.get_doc("Virtual Machine", proxy_vm_name)
	connection = connection_for_guest(vm)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		live, stderr, code = run_ssh(
			connection, key_path, proxy._curl_command("GET", "/map"), timeout_seconds=60
		)
	assert code == 0, f"reading proxy /map failed: {stderr}"
	return json.loads(live) if live.strip() else {}


def _cleanup(*labels: str) -> None:
	for label in labels:
		if frappe.db.exists("Subdomain", label):
			frappe.delete_doc("Subdomain", label, force=1, ignore_permissions=True)
	frappe.db.commit()
