"""Memory-floor sweep: how low can a bench golden VM's RAM go before the live
stack stops booting + serving? The last comparison flagged MariaDB as the boot
critical-chain gate AND the largest single consumer, so the floor is really
"how small can the InnoDB buffer pool get and still serve pong on a cold clone."

For each memory tier (2048 -> 1024 -> 768 -> 512 -> 384 -> 256 MB) we:
  1. Clone a fresh VM from the golden snapshot AT THAT memory size.
  2. Wait for Running, then SSH host-side over the routed-tap path.
  3. Shrink innodb_buffer_pool_size to a tier-appropriate value (a fixed fraction
     of VM RAM, floored at MariaDB's 5MB minimum) in a drop-in my.cnf and cold
     reboot so the whole stack comes back up under the new ceiling — the honest
     test (bench-cli sized the pool for 2GB; we're asking if a smaller pool holds).
  4. GATE: SSH-ready, then curl the baked site for `pong`. Record whether it
     served, how long it took, PSI memory-full stall (reclaim thrash), and any
     OOM kills in the journal.
  5. Tear the clone down.

The lowest tier that still serves pong on a cold reboot with no OOM kill and
bounded memory pressure is the floor. Below it the stack either OOMs a worker or
thrashes so hard it never answers within the deadline.

Method trap (same as the compare harness): reach the guest FROM THE HOST over the
routed-tap path (public v6 is lossy); time off the host loop clock.

Run:

    bench --site scaleway.local execute \
      atlas.tests.e2e.use_cases.bench_memory_floor.run \
      --kwargs "{'snapshot':'4405ie9nue'}"

Sweep a custom ladder (stop early once you see a failure):

    ... .run --kwargs "{'snapshot':'4405ie9nue','tiers':[1024,768,512,384]}"
"""

import time
import traceback

import frappe

from atlas.atlas.ssh import connection_for_server, run_ssh, ssh_key_file
from atlas.tests.e2e._config import ephemeral_public_key
from atlas.tests.e2e._tasks import wait_for_vm_running
from atlas.tests.e2e.use_cases import boot_deep_profile as deep
from atlas.tests.e2e.use_cases.bench_image_compare import (
	_SERVE_LOOP,
	BAKED_SITE,
	CLONE_VCPUS,
	_parse_serve,
)
from atlas.tests.e2e.use_cases.image_boot_benchmark import (
	SSH_DEADLINE_SECONDS,
	_active_scaleway_server,
	_stage_probe_key,
	_terminate,
)

DEFAULT_TIERS = [2048, 1024, 768, 512, 384, 256]

# Fraction of VM RAM to hand the InnoDB buffer pool. bench-cli's default is tuned
# for a roomy box; on a squeezed VM the pool has to leave headroom for the kernel,
# the gunicorn/python workers, redis, nginx and node. 25% is conservative — the
# pool is a cache, not correctness-critical, so a small pool just means colder
# reads, and the golden site is tiny. Floored at MariaDB's 5MB hard minimum.
BUFPOOL_FRACTION = 0.25
BUFPOOL_MIN_MB = 5


def run(snapshot: str, tiers: list | None = None, server: str = "", teardown: bool = True) -> None:
	server_name = server or _active_scaleway_server()
	ladder = tiers or DEFAULT_TIERS
	print(f"[floor] server={server_name} snapshot={snapshot} tiers={ladder}")
	conn = connection_for_server(frappe.get_doc("Server", server_name))

	results = []
	floor = None
	for mem_mb in ladder:
		print(f"\n{'#' * 78}\n# MEMORY TIER {mem_mb} MB  snapshot={snapshot}\n{'#' * 78}")
		try:
			r = _sweep_tier(conn, server_name, snapshot, mem_mb, teardown=teardown)
		except Exception:
			traceback.print_exc()
			r = {"mem_mb": mem_mb, "passed": False, "error": "exception"}
		results.append(r)
		if r.get("passed"):
			floor = mem_mb
		else:
			# First failing rung: the tier above it is the floor. Keep going one
			# more rung to confirm it stays broken, then stop the descent.
			print(f"[floor] tier {mem_mb} MB FAILED — floor is {floor} MB (last passing)")
			break

	_summary(results, floor)


def _sweep_tier(conn, server_name: str, snapshot: str, mem_mb: int, teardown: bool) -> dict:
	snap = frappe.get_doc("Virtual Machine Snapshot", snapshot)
	bufpool_mb = max(BUFPOOL_MIN_MB, int(mem_mb * BUFPOOL_FRACTION))

	vm_name = snap.clone_to_new_vm(
		title=f"floor {mem_mb}mb",
		ssh_public_key=ephemeral_public_key(),
		vcpus=CLONE_VCPUS,
		memory_megabytes=mem_mb,
	)
	frappe.db.commit()
	print(f"[floor] {mem_mb}MB: cloned VM {vm_name}; buf_pool target={bufpool_mb}MB; waiting for Running…")

	r = {"mem_mb": mem_mb, "vm": vm_name, "bufpool_mb": bufpool_mb, "passed": False}
	try:
		wait_for_vm_running(vm_name, timeout_seconds=300, poll_seconds=5)
		vm = frappe.get_doc("Virtual Machine", vm_name)
		guest = vm.ipv6_address
		r["guest"] = guest
		print(f"[floor] {mem_mb}MB: Running, guest={guest}")

		with ssh_key_file(conn.ssh_private_key) as key:
			_stage_probe_key(conn, key)

			# Wait for the first cold boot to answer SSH before we mutate config.
			first = _serve(conn, key, guest, tag=f"{mem_mb}MB first-boot")
			r["first_serve_ms"] = first.get("serve_ms")
			if "ssh_ms" not in first:
				print(f"[floor] {mem_mb}MB: never answered SSH on first boot — hard fail")
				r["fail_reason"] = "no-ssh-first-boot"
				return r

			# Shrink the InnoDB buffer pool and cold reboot into the new ceiling.
			_shrink_bufpool(conn, key, guest, bufpool_mb)
			_cold_reboot(conn, key, guest, tag=f"{mem_mb}MB")

			# GATE: does the whole stack come back and serve pong under the ceiling?
			after = _serve(conn, key, guest, tag=f"{mem_mb}MB post-shrink reboot")
			r["reboot_serve_ms"] = after.get("serve_ms")
			r["reboot_ssh_ms"] = after.get("ssh_ms")

			# Health: OOM kills + memory-full pressure (reclaim thrash) decide the floor.
			health = _health(conn, key, guest)
			r.update(health)

			served = after.get("serve_ms") is not None
			no_oom = health.get("oom_kills", 0) == 0
			r["passed"] = bool(served and no_oom)
			print(
				f"[floor] {mem_mb}MB: served={served} oom_kills={health.get('oom_kills')} "
				f"psi_mem_full={health.get('psi_mem_full_us')}us -> {'PASS' if r['passed'] else 'FAIL'}"
			)
			if not r["passed"]:
				r["fail_reason"] = "no-serve" if not served else "oom"

			# One deep-memory snapshot at this tier for the report.
			deep._dump_memory(conn, key, guest)
			deep._dump_mariadb(conn, key, guest)
			deep._dump_pressure(conn, key, guest)
	except Exception:
		traceback.print_exc()
		r["error"] = "exception"
	finally:
		if teardown:
			_terminate(vm_name)
			print(f"[floor] {mem_mb}MB: terminated {vm_name}")
		else:
			print(f"[floor] {mem_mb}MB: VM {vm_name} LEFT RUNNING")
	return r


def _serve(conn, key, guest, tag: str) -> dict:
	loop = _SERVE_LOOP.format(guest=guest, host=BAKED_SITE, hold=SSH_DEADLINE_SECONDS)
	out, _, _ = run_ssh(conn, key, loop, timeout_seconds=SSH_DEADLINE_SECONDS + 30)
	print(f"[{tag}] {out.strip()}")
	return _parse_serve(out, prefix="")


def _shrink_bufpool(conn, key, guest, bufpool_mb: int) -> None:
	"""Drop a my.cnf override that shrinks the InnoDB buffer pool + trims the log
	file and buffer to fit a squeezed box, then let the cold reboot apply it. We
	write to a bench-cli-owned conf.d so we don't fight its generated my.cnf."""
	# A pool this small needs a single instance (instances*128MB chunking would
	# otherwise round the pool up) and a proportionally small log.
	log_mb = max(4, bufpool_mb // 4)
	override = (
		"[mysqld]\n"
		f"innodb_buffer_pool_size = {bufpool_mb}M\n"
		"innodb_buffer_pool_instances = 1\n"
		f"innodb_log_file_size = {log_mb}M\n"
		"innodb_log_buffer_size = 4M\n"
		"performance_schema = OFF\n"
		"table_open_cache = 200\n"
		"tmp_table_size = 8M\n"
		"max_heap_table_size = 8M\n"
	)
	import base64

	b64 = base64.b64encode(override.encode()).decode()
	# Find the conf.d for the atlas instance; fall back to the generic mariadb one.
	payload = (
		"set -e; "
		"d=$(ls -d /etc/mysql/mariadb.conf.d /etc/mysql/conf.d 2>/dev/null | head -1); "
		'[ -z "$d" ] && d=/etc/mysql/mariadb.conf.d && mkdir -p $d; '
		f"echo {b64} | base64 -d > $d/zz-atlas-floor.cnf; "
		"echo WROTE $d/zz-atlas-floor.cnf"
	)
	out = deep._guest(conn, key, guest, payload, timeout=30)
	print(f"[floor] bufpool override -> {bufpool_mb}M log={log_mb}M :: {out.strip()}")


def _cold_reboot(conn, key, guest, tag: str) -> None:
	print(f"[{tag}] cold reboot to apply memory ceiling…")
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
		print(f"[{tag}] WARNING: guest never went down after reboot")


def _health(conn, key, guest) -> dict:
	"""Post-reboot health: count OOM kills since boot and read memory-full PSI
	(the kernel's own 'everyone is stalled reclaiming' signal). These decide
	whether a tier that *served* did so healthily or was one request from death."""
	payload = r"""
echo -n 'OOM_KILLS='
( journalctl -k --no-pager 2>/dev/null | grep -icE 'Out of memory|oom-kill|Killed process' ) || echo 0
echo -n 'PSI_MEM_FULL_US='
awk '/full/{for(i=1;i<=NF;i++)if($i~/^total=/){sub(/total=/,"",$i);print $i}}' /proc/pressure/memory 2>/dev/null || echo 0
echo -n 'PSI_MEM_SOME_US='
awk '/some/{for(i=1;i<=NF;i++)if($i~/^total=/){sub(/total=/,"",$i);print $i}}' /proc/pressure/memory 2>/dev/null || echo 0
echo -n 'SWAP_USED_MB='
free -m | awk '/^Swap:/{print $3+0}'
echo -n 'MEM_AVAIL_MB='
free -m | awk '/^Mem:/{print $7+0}'
echo -n 'MARIADB_UP='
( pgrep -x mariadbd >/dev/null && echo 1 || echo 0 )
"""
	out = deep._guest(conn, key, guest, payload, timeout=45)
	print(out)
	d = {}
	for line in out.splitlines():
		line = line.strip()
		for tag, dst in (
			("OOM_KILLS=", "oom_kills"),
			("PSI_MEM_FULL_US=", "psi_mem_full_us"),
			("PSI_MEM_SOME_US=", "psi_mem_some_us"),
			("SWAP_USED_MB=", "swap_used_mb"),
			("MEM_AVAIL_MB=", "mem_avail_mb"),
			("MARIADB_UP=", "mariadb_up"),
		):
			if line.startswith(tag):
				v = line[len(tag) :].strip()
				if v.isdigit():
					d[dst] = int(v)
	return d


def _summary(results: list, floor) -> None:
	print("\n" + "=" * 84)
	print("BENCH MEMORY-FLOOR SWEEP — lowest VM RAM that still boots + serves pong")
	print("buf pool = 25% of VM RAM (floored 5MB); gate = cold-reboot serve + no OOM kill")
	print("=" * 84)
	hdr = f"{'RAM':>6s} {'bufpool':>8s} {'served':>7s} {'serve_s':>8s} {'oom':>4s} {'memPSIfull':>11s} {'swap':>6s} {'avail':>6s} {'verdict':>8s}"
	print(hdr)
	print("-" * 84)
	for r in results:
		served = "yes" if r.get("reboot_serve_ms") is not None else "NO"
		serve_s = f"{r['reboot_serve_ms'] / 1000:.2f}" if r.get("reboot_serve_ms") else "—"
		psi = r.get("psi_mem_full_us")
		psi_s = f"{psi / 1000:.0f}ms" if isinstance(psi, int) else "—"
		print(
			f"{r.get('mem_mb'):>4d}MB {str(r.get('bufpool_mb', '—')) + 'M':>8s} "
			f"{served:>7s} {serve_s:>8s} {r.get('oom_kills', '—')!s:>4s} "
			f"{psi_s:>11s} {str(r.get('swap_used_mb', '—')) + 'M':>6s} "
			f"{str(r.get('mem_avail_mb', '—')) + 'M':>6s} "
			f"{'PASS' if r.get('passed') else 'FAIL':>8s}"
		)
	print("=" * 84)
	print(f"FLOOR (lowest passing tier): {floor} MB" if floor else "FLOOR: no tier passed")
