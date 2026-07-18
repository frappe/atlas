"""Reproduce the 512MB memory-floor failure with a WATCHED reboot: clone
fresh, shrink the InnoDB buffer pool, issue the reboot, then sample host-side
firecracker RSS (guest RAM actually faulted in) and guest reachability every
few seconds across the whole reboot — instead of waiting out one opaque 240s
serve-deadline blind. Shows whether RAM climbs to the ceiling and boot stalls
there (memory-bound OOM/thrash) or the guest panics/reboots quickly and
repeatedly (something else, e.g. corrupted state).

No serial console is available (Firecracker boot_args set nr_uarts=0
deliberately, to avoid flooding firecracker.log — see provision-vm.py), so
this is the closest live signal we get without mounting the guest disk.

Run:

    bench --site scaleway.local execute \
      atlas.tests.e2e.use_cases.mem_failure_probe_host.run \
      --kwargs "{'snapshot':'4405ie9nue','mem_mb':512}"
"""

import time

import frappe

from atlas.atlas.ssh import connection_for_server, run_ssh, ssh_key_file
from atlas.tests.e2e._config import ephemeral_public_key
from atlas.tests.e2e._tasks import wait_for_vm_running
from atlas.tests.e2e.use_cases.bench_image_compare import CLONE_VCPUS
from atlas.tests.e2e.use_cases.bench_memory_floor import BUFPOOL_FRACTION, BUFPOOL_MIN_MB, _shrink_bufpool
from atlas.tests.e2e.use_cases.image_boot_benchmark import (
	_active_scaleway_server,
	_stage_probe_key,
	_terminate,
)


def run(snapshot: str, mem_mb: int = 512, server: str = "", teardown: bool = True) -> None:
	server_name = server or _active_scaleway_server()
	conn = connection_for_server(frappe.get_doc("Server", server_name))
	snap = frappe.get_doc("Virtual Machine Snapshot", snapshot)
	bufpool_mb = max(BUFPOOL_MIN_MB, int(mem_mb * BUFPOOL_FRACTION))
	print(f"[watch] server={server_name} snapshot={snapshot} mem_mb={mem_mb} bufpool_target={bufpool_mb}MB")

	vm_name = snap.clone_to_new_vm(
		title=f"watch {mem_mb}mb",
		ssh_public_key=ephemeral_public_key(),
		vcpus=CLONE_VCPUS,
		memory_megabytes=mem_mb,
	)
	frappe.db.commit()
	print(f"[watch] cloned VM {vm_name}; waiting for Running…")

	try:
		wait_for_vm_running(vm_name, timeout_seconds=300, poll_seconds=5)
		vm = frappe.get_doc("Virtual Machine", vm_name)
		guest = vm.ipv6_address
		print(f"[watch] Running, guest={guest}")

		with ssh_key_file(conn.ssh_private_key) as key:
			_stage_probe_key(conn, key)

			print("[watch] waiting for first-boot SSH…")
			_wait_ssh(conn, key, guest, deadline=60)

			_shrink_bufpool(conn, key, guest, bufpool_mb)

			print("[watch] issuing reboot…")
			reboot_cmd = (
				f"ssh -i /tmp/hp.key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
				f"-o BatchMode=yes -o ConnectTimeout=5 root@{guest} "
				f"'systemd-run --on-active=1 systemctl reboot' 2>&1; echo ISSUED_rc=$?"
			)
			out, _, _ = run_ssh(conn, key, reboot_cmd, timeout_seconds=20)
			print(out)

			_watch(conn, key, vm_name, guest, samples=50, interval=5)
	finally:
		if teardown:
			_terminate(vm_name)
			print(f"[watch] terminated {vm_name}")
		else:
			print(f"[watch] VM {vm_name} LEFT RUNNING")


def _wait_ssh(conn, key, guest, deadline: int) -> None:
	payload = (
		f"end=$(( $(date +%s) + {deadline} )); "
		f"while [ $(date +%s) -lt $end ]; do "
		f"  timeout 3 ssh -i /tmp/hp.key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
		f"    -o BatchMode=yes -o ConnectTimeout=2 root@{guest} true 2>/dev/null && echo READY && break; "
		f"  sleep 1; done"
	)
	out, _, _ = run_ssh(conn, key, payload, timeout_seconds=deadline + 15)
	print(f"[watch] first-boot ssh: {out.strip() or '(timeout)'}")


def _watch(conn, key, vm_name: str, guest: str, samples: int, interval: int) -> None:
	print(f"\n{'sample':>6} {'elapsed_s':>10} {'fc_rss_kb':>10} {'tcp22':>6} {'ssh':>6}")
	t0 = time.time()
	for i in range(samples):
		time.sleep(interval)
		elapsed = int(time.time() - t0)
		payload = f"""
set +e
pid=$(sudo pgrep -af firecracker | grep {vm_name} | awk '{{print $1}}' | head -1)
rss=$(sudo awk '/^Rss:/{{print $2}}' /proc/$pid/smaps_rollup 2>/dev/null)
tcp=$(timeout 2 bash -c "exec 3<>/dev/tcp/{guest}/22" 2>/dev/null && echo 1 || echo 0)
ssh=$(timeout 3 ssh -i /tmp/hp.key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=2 root@{guest} true 2>/dev/null && echo 1 || echo 0)
echo "RSS=$rss TCP=$tcp SSH=$ssh PID=$pid"
"""
		out, _, _ = run_ssh(conn, key, payload, timeout_seconds=15)
		rss = tcp = ssh = pid = "?"
		for line in out.splitlines():
			line = line.strip()
			if line.startswith("RSS="):
				parts = dict(kv.split("=", 1) for kv in line.split() if "=" in kv)
				rss = parts.get("RSS", "?")
				tcp = parts.get("TCP", "?")
				ssh = parts.get("SSH", "?")
				pid = parts.get("PID", "?")
		print(f"{i:>6} {elapsed:>10} {rss:>10} {tcp:>6} {ssh:>6}  pid={pid}")
		if ssh == "1":
			print("[watch] SSH came back — stopping early, guest recovered")
			break
	else:
		print("[watch] never came back within sample budget")
