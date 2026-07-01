"""Autopsy the 512MB memory-floor FAILURE from bench_memory_floor.run: who is
actually eating RAM when the golden won't come back up after a shrunk-pool
cold reboot? The floor sweep only records pass/fail + a post-hoc dump; this
clones at a target tier, shrinks the buffer pool, reboots, and — whether or
not it serves pong — dumps a full memory autopsy (per-process PSS, per-cgroup
slice, meminfo, OOM journal, dmesg tail, systemd unit states) BEFORE tearing
down, so a stuck/thrashing/OOM'd guest gets read live instead of guessed at.

Run:

    bench --site scaleway.local execute \
      atlas.tests.e2e.use_cases.mem_failure_autopsy.run \
      --kwargs "{'snapshot':'4405ie9nue','mem_mb':512}"
"""

import traceback

import frappe

from atlas.atlas.ssh import connection_for_server, run_ssh, ssh_key_file
from atlas.tests.e2e._config import ephemeral_public_key
from atlas.tests.e2e._tasks import wait_for_vm_running
from atlas.tests.e2e.use_cases import boot_deep_profile as deep
from atlas.tests.e2e.use_cases.bench_image_compare import _SERVE_LOOP, BAKED_SITE, CLONE_VCPUS, _parse_serve
from atlas.tests.e2e.use_cases.bench_memory_floor import BUFPOOL_FRACTION, BUFPOOL_MIN_MB, _shrink_bufpool
from atlas.tests.e2e.use_cases.image_boot_benchmark import (
	SSH_DEADLINE_SECONDS,
	_active_scaleway_server,
	_stage_probe_key,
	_terminate,
)


def run(snapshot: str, mem_mb: int = 512, server: str = "", teardown: bool = False) -> None:
	server_name = server or _active_scaleway_server()
	conn = connection_for_server(frappe.get_doc("Server", server_name))
	snap = frappe.get_doc("Virtual Machine Snapshot", snapshot)
	bufpool_mb = max(BUFPOOL_MIN_MB, int(mem_mb * BUFPOOL_FRACTION))
	print(f"[autopsy] server={server_name} snapshot={snapshot} mem_mb={mem_mb} bufpool_target={bufpool_mb}MB")

	vm_name = snap.clone_to_new_vm(
		title=f"autopsy {mem_mb}mb",
		ssh_public_key=ephemeral_public_key(),
		vcpus=CLONE_VCPUS,
		memory_megabytes=mem_mb,
	)
	frappe.db.commit()
	print(f"[autopsy] cloned VM {vm_name}; waiting for Running…")

	try:
		wait_for_vm_running(vm_name, timeout_seconds=300, poll_seconds=5)
		vm = frappe.get_doc("Virtual Machine", vm_name)
		guest = vm.ipv6_address
		print(f"[autopsy] Running, guest={guest}")

		with ssh_key_file(conn.ssh_private_key) as key:
			_stage_probe_key(conn, key)

			first = _serve(conn, key, guest, tag="first-boot (default bufpool)")
			if "ssh_ms" not in first:
				print("[autopsy] never answered SSH on first boot — aborting")
				return

			print("\n" + "=" * 78)
			print(f"BASELINE — default config, {mem_mb}MB RAM, before shrinking anything")
			print("=" * 78)
			deep._dump_memory(conn, key, guest)
			deep._dump_cgroup_memory(conn, key, guest)
			deep._dump_processes(conn, key, guest)
			deep._dump_mariadb(conn, key, guest)

			_shrink_bufpool(conn, key, guest, bufpool_mb)
			_cold_reboot(conn, key, guest)

			after = _serve(conn, key, guest, tag="post-shrink reboot")
			served = after.get("serve_ms") is not None
			print(f"\n[autopsy] post-shrink reboot served={served}")

			print("\n" + "#" * 78)
			print(f"# AUTOPSY — {mem_mb}MB RAM, bufpool={bufpool_mb}MB, served={served}")
			print("#" * 78)

			_dump_oom(conn, key, guest)
			_dump_dmesg_tail(conn, key, guest)
			deep._dump_memory(conn, key, guest)
			deep._dump_cgroup_memory(conn, key, guest)
			deep._dump_processes(conn, key, guest)
			deep._dump_mariadb(conn, key, guest)
			deep._dump_pressure(conn, key, guest)
			_dump_unit_states(conn, key, guest)
	except Exception:
		traceback.print_exc()
	finally:
		if teardown:
			_terminate(vm_name)
			print(f"[autopsy] terminated {vm_name}")
		else:
			print(f"\n[autopsy] VM {vm_name} LEFT RUNNING for further poking — _terminate('{vm_name}')")


def _serve(conn, key, guest, tag: str) -> dict:
	loop = _SERVE_LOOP.format(guest=guest, host=BAKED_SITE, hold=SSH_DEADLINE_SECONDS)
	out, _, _ = run_ssh(conn, key, loop, timeout_seconds=SSH_DEADLINE_SECONDS + 30)
	print(f"[{tag}] {out.strip()}")
	return _parse_serve(out, prefix="")


def _cold_reboot(conn, key, guest) -> None:
	print("[autopsy] cold reboot to apply shrunk bufpool…")
	down = (
		f"g={guest}; "
		f"ssh -i /tmp/hp.key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
		f"-o BatchMode=yes -o ConnectTimeout=5 root@$g 'systemd-run --on-active=1 systemctl reboot' 2>/dev/null; "
		f"for i in $(seq 1 60); do "
		f'  if ! timeout 2 bash -c "exec 3<>/dev/tcp/$g/22" 2>/dev/null; then echo DOWN; break; fi; '
		f"  sleep 1; done"
	)
	out, _, _ = run_ssh(conn, key, down, timeout_seconds=90)
	if "DOWN" not in out:
		print("[autopsy] WARNING: guest never went down after reboot")


def _dump_oom(conn, key, guest) -> None:
	print("\n" + "=" * 78 + "\nOOM killer journal (who died, and what the reaper saw)\n" + "=" * 78)
	payload = r"""
journalctl -k --no-pager 2>/dev/null | grep -iE 'Out of memory|oom-kill|Killed process|invoked oom-killer' | tail -40
echo '--- full oom-killer context blocks (mem sizes at kill time) ---'
journalctl -k --no-pager 2>/dev/null | grep -B2 -A30 'invoked oom-killer' | tail -120
"""
	print(deep._guest(conn, key, guest, payload, timeout=30))


def _dump_dmesg_tail(conn, key, guest) -> None:
	print(
		"\n" + "=" * 78 + "\ndmesg tail (last 60 lines — boot stall / reclaim / kill evidence)\n" + "=" * 78
	)
	print(deep._guest(conn, key, guest, "dmesg 2>/dev/null | tail -60", timeout=30))


def _dump_unit_states(conn, key, guest) -> None:
	print("\n" + "=" * 78 + "\nsystemd unit states (what's still starting/failed post-reboot)\n" + "=" * 78)
	payload = r"""
echo '--- failed ---'
systemctl --failed --no-legend --no-pager 2>/dev/null || echo '(none)'
echo '--- not-active/activating (stuck) ---'
systemctl list-units --all --no-legend --no-pager 2>/dev/null | grep -viE '\bactive\b.*\brunning\b|\bactive\b.*\bexited\b|\bactive\b.*\bplugged\b|\bactive\b.*\blistening\b|\bactive\b.*\bmounted\b|\bactive\b.*\bwaiting\b' | head -30
echo '--- mariadb unit status ---'
systemctl status mariadb@atlas --no-pager -l 2>/dev/null | head -30
"""
	print(deep._guest(conn, key, guest, payload))
