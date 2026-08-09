"""Compare two GOLDEN BENCH images baked on the two optimized bases
(standard-optimized vs minimal-optimized).

The base-image comparison (bare boot) was already done — minimal wins on disk
and package surface, ties on memory/boot. This asks the harder question the last
session flagged: once you bake the full bench stack (MariaDB + redis + nginx +
gunicorn workers, all `systemctl --user` under linger) ON TOP of each base and
actually SERVE requests, do the two goldens still tie? Or does the standard
image's extra distro baggage cost something once real workloads run?

For each golden snapshot we:
  1. Clone a fresh VM from it (new identity, boots the baked stack).
  2. Wait host-side (routed-tap) for SSH, then for the site to serve `pong`.
     -> serve_ms: SSH-ready -> first 200 on /api/method/ping. "Time to first
        request served" after a cold clone boot.
  3. Dump the full deep profile off the guest's own clocks (systemd-analyze,
     critical-chain, blame, per-proc PSS/RSS/USS, CPU burned, disk, packages,
     units, host-side firecracker RSS).
  4. COLD REBOOT the guest, then re-time SSH-ready and serve-ready off the
     reboot (reboot_ssh_ms / reboot_serve_ms) — the steady-state restart the
     base-image bare-boot number can't show (the stack must come back up).
  5. Tear the clone down.

Method trap (same as image_boot_benchmark): reach the guest FROM THE HOST over
the routed-tap path; public v6 is lossy. Read the guest's OWN monotonic clocks
for boot markers; time serve/reboot latency off the HOST's loop clock (a single
SSH round trip owns the loop so there's no per-probe SSH cost).

Run (site VMs need real resources — the stack is live):

    bench --site scaleway.local execute \
      atlas.tests.e2e.use_cases.bench_image_compare.run \
      --kwargs "{'standard_snapshot':'<snap>','minimal_snapshot':'<snap>'}"

Or profile one snapshot:

    ... .profile_one --kwargs "{'snapshot':'<snap>','label':'standard'}"
"""

import base64
import time
import traceback

import frappe

from atlas.atlas.ssh import connection_for_server, run_ssh, ssh_key_file
from atlas.tests.e2e._config import ephemeral_private_key, ephemeral_public_key
from atlas.tests.e2e._tasks import wait_for_vm_running
from atlas.tests.e2e.use_cases import boot_deep_profile as deep
from atlas.tests.e2e.use_cases.image_boot_benchmark import (
	_HOST_WAIT,
	SSH_DEADLINE_SECONDS,
	_active_scaleway_server,
	_stage_probe_key,
	_terminate,
)

# Clones run the live bench stack; the bare-boot 512MB/4GB sizing is far too small.
# Match the bench recipe's build-VM sizing so the stack has the RAM + disk it baked
# with (the snapshot's rootfs is already grown to 28 GB).
CLONE_VCPUS = 2
CLONE_MEMORY_MB = 2048
BAKED_SITE = "site.local"  # bench/build.sh BAKED_SITE — the site the golden serves


def run(standard_snapshot: str, minimal_snapshot: str, server: str = "", teardown: bool = True) -> None:
	server_name = server or _active_scaleway_server()
	print(f"[compare] server={server_name}")
	print(f"[compare] standard={standard_snapshot} minimal={minimal_snapshot}")

	results = {}
	for label, snap in (("standard", standard_snapshot), ("minimal", minimal_snapshot)):
		print(f"\n{'#' * 78}\n# {label.upper()} golden  snapshot={snap}\n{'#' * 78}")
		try:
			results[label] = _profile_snapshot(server_name, snap, label, teardown=teardown)
		except Exception:
			print(f"[compare] {label} FAILED")
			traceback.print_exc()
			results[label] = None

	_summary(results)


def profile_one(snapshot: str, label: str = "image", server: str = "", teardown: bool = False) -> dict:
	server_name = server or _active_scaleway_server()
	return _profile_snapshot(server_name, snapshot, label, teardown=teardown)


def compare_bench_vs_base(
	bench_snapshot: str, base_image: str, server: str = "", teardown: bool = True
) -> None:
	"""Answer 'is SSH/network slow BECAUSE of bench?' Clone a bench golden and a
	bare base image (no bench stack) from the SAME start and time ping-ready,
	TCP:22-open, and SSH-ready for each. The base can't serve pong; the point is
	the network/SSH readiness gap. If the base SSHes much faster than the bench
	golden, the bench stack's boot-time CPU contention (baking workers, MariaDB
	warmup) — not the network — is what pushes SSH out. Both cloned at the SAME
	sizing so the vCPU budget is identical and the only variable is the payload."""
	server_name = server or _active_scaleway_server()
	conn = connection_for_server(frappe.get_doc("Server", server_name))
	print(f"[bench-vs-base] bench={bench_snapshot} base={base_image} server={server_name}")

	results = {}
	# Bench golden: full serve timing (reuses the snapshot clone path).
	results["bench"] = _profile_snapshot(server_name, bench_snapshot, "bench", teardown=teardown)
	# Bare base: provision from image (no stack); time ping/tcp/ssh only.
	results["base"] = _profile_base_net(conn, server_name, base_image, teardown=teardown)

	print("\n" + "=" * 78)
	print("SSH / NETWORK READINESS — bench golden vs bare base (same host, same sizing)")
	print("=" * 78)
	print(f"{'marker':24s} {'bench':>14s} {'base':>14s} {'delta(bench-base)':>18s}")
	print("-" * 78)
	for key, label in (
		("ping_ms", "ping-ready"),
		("tcp22_ms", "TCP:22 open"),
		("ssh_ms", "SSH-ready"),
	):
		b, a = results["bench"].get(key), results["base"].get(key)
		bf = f"{b / 1000:.2f}s" if b is not None else "—"
		af = f"{a / 1000:.2f}s" if a is not None else "—"
		df = f"{(b - a) / 1000:+.2f}s" if (b is not None and a is not None) else "—"
		print(f"{label:24s} {bf:>14s} {af:>14s} {df:>18s}")
	print("=" * 78)
	print("If SSH-ready delta >> ping/TCP delta, the network is fine — bench's boot-time")
	print("CPU contention is what delays sshd, not networking.")


def _profile_base_net(conn, server_name: str, base_image: str, teardown: bool) -> dict:
	"""Provision a bare base VM (no bench) at the bench sizing and time net/SSH
	readiness only. Uses the deep-profile provision path (image, not snapshot)."""
	from atlas.tests.e2e.use_cases.image_boot_benchmark import _wait_for_provision_task

	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": f"net-base {base_image}",
			"server": server_name,
			"image": base_image,
			"vcpus": CLONE_VCPUS,
			"memory_megabytes": CLONE_MEMORY_MB,
			"disk_gigabytes": 4,
			"ssh_public_key": ephemeral_public_key(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	vm_name = vm.name
	print(f"[bench-vs-base] base VM {vm_name} inserted; waiting for provision…")
	d = {"label": "base", "vm": vm_name}
	try:
		_wait_for_provision_task(vm_name, timeout_seconds=180)
		vm.reload()
		guest = vm.ipv6_address
		d["guest"] = guest
		with ssh_key_file(conn.ssh_private_key) as key:
			_stage_probe_key(conn, key)
			# No baked site on a bare base -> host header is irrelevant; serve never fires.
			loop = _SERVE_LOOP.format(guest=guest, host="unused", hold=SSH_DEADLINE_SECONDS)
			out, _, _ = run_ssh(conn, key, loop, timeout_seconds=SSH_DEADLINE_SECONDS + 30)
			print(f"[bench-vs-base base] {out.strip()}")
			d.update(_parse_serve(out, prefix=""))
			deep._dump_net_ssh(conn, key, guest)
	except Exception:
		traceback.print_exc()
	finally:
		if teardown:
			_terminate(vm_name)
			print(f"[bench-vs-base] base terminated {vm_name}")
		else:
			print(f"[bench-vs-base] base VM {vm_name} LEFT RUNNING")
	return d


def _profile_snapshot(server_name: str, snapshot: str, label: str, teardown: bool) -> dict:
	conn = connection_for_server(frappe.get_doc("Server", server_name))
	snap = frappe.get_doc("Virtual Machine Snapshot", snapshot)

	# Trust both keys: deep-profile dumps use the ephemeral key staged at /tmp/hp.key.
	vm_name = snap.clone_to_new_vm(
		title=f"compare {label}",
		ssh_public_key=ephemeral_public_key(),
		vcpus=CLONE_VCPUS,
		memory_megabytes=CLONE_MEMORY_MB,
	)
	frappe.db.commit()
	print(f"[compare] {label}: cloned VM {vm_name} from {snapshot}; waiting for Running…")

	summary = {"label": label, "snapshot": snapshot, "vm": vm_name}
	try:
		wait_for_vm_running(vm_name, timeout_seconds=300, poll_seconds=5)
		vm = frappe.get_doc("Virtual Machine", vm_name)
		guest = vm.ipv6_address
		summary["guest"] = guest
		print(f"[compare] {label}: VM Running, guest={guest}")

		with ssh_key_file(conn.ssh_private_key) as key:
			_stage_probe_key(conn, key)

			# 1. Cold-clone: SSH-ready then serve-ready (time to first pong).
			serve = _time_ssh_and_serve(conn, key, guest, tag=f"{label} cold")
			summary.update(serve)

			# 2. Full deep profile off the guest's own clocks.
			print(f"\n{'#' * 78}\n# DEEP PROFILE — {label} vm={vm_name} guest={guest}\n{'#' * 78}")
			deep._dump_analyze(conn, key, guest)
			deep._dump_critical_chain(conn, key, guest)
			deep._dump_blame(conn, key, guest)
			deep._dump_kernel_timeline(conn, key, guest)
			deep._dump_units(conn, key, guest)
			deep._dump_processes(conn, key, guest)
			deep._dump_cpu(conn, key, guest)
			deep._dump_boot_bound(conn, key, guest)
			deep._dump_pressure(conn, key, guest)
			deep._dump_memory(conn, key, guest)
			deep._dump_cgroup_memory(conn, key, guest)
			deep._dump_mariadb(conn, key, guest)
			deep._dump_modules(conn, key, guest)
			deep._dump_disk(conn, key, guest)
			deep._dump_net_ssh(conn, key, guest)
			deep._dump_packages(conn, key, guest)
			deep._dump_package_classes(conn, key, guest)
			deep._dump_host_overhead(conn, key, vm_name, guest)

			# Capture the compact machine-readable markers too, for the summary table.
			summary.update(_collect_markers(conn, key, guest))

			# 3. Cold reboot: does the stack come back and serve, and how fast?
			reboot = _cold_reboot_and_time(conn, key, guest, tag=f"{label} reboot")
			summary.update(reboot)
	except Exception:
		traceback.print_exc()
	finally:
		if teardown:
			_terminate(vm_name)
			print(f"[compare] {label}: terminated {vm_name}")
		else:
			print(f"[compare] {label}: VM {vm_name} LEFT RUNNING — _terminate_by_name('{vm_name}')")
	return summary


def _terminate_by_name(vm_name: str) -> None:
	_terminate(vm_name)
	print(f"[compare] terminated {vm_name}")


# --- timing helpers (host loop clock authoritative) -------------------------


def _time_ssh_and_serve(conn, key, guest, tag: str) -> dict:
	"""One host-side loop: measure elapsed to (a) SSH answers and (b) curl of
	http://[guest]/api/method/ping returns 'pong', both from the SAME start. The
	guest is booting during/after the clone provision, so these are 'from when the
	host first started probing' — a lower bound on cold-boot-to-serving that is
	consistent between the two images (same start condition)."""
	loop = _SERVE_LOOP.format(guest=guest, host=BAKED_SITE, hold=SSH_DEADLINE_SECONDS)
	out, _, _ = run_ssh(conn, key, loop, timeout_seconds=SSH_DEADLINE_SECONDS + 30)
	print(f"[{tag}] {out.strip()}")
	return _parse_serve(out, prefix="")


def _cold_reboot_and_time(conn, key, guest, tag: str) -> dict:
	"""Issue `reboot` on the guest, wait for it to go down, then time SSH-ready and
	serve-ready off the reboot. This is the number the base-image bare boot can't
	show: the full stack (MariaDB/redis/nginx/gunicorn) must come back up and serve."""
	print(f"[{tag}] issuing cold reboot…")
	# Fire reboot (backgrounded so ssh returns), then poll for the port to drop.
	down = (
		f"g={guest}; "
		f"ssh -i /tmp/hp.key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
		f"-o BatchMode=yes -o ConnectTimeout=5 root@$g 'systemd-run --on-active=1 systemctl reboot' 2>/dev/null; "
		# Wait until SSH stops answering (guest actually went down), max 60s.
		f"for i in $(seq 1 60); do "
		f'  if ! timeout 2 bash -c "exec 3<>/dev/tcp/$g/22" 2>/dev/null; then echo DOWN; break; fi; '
		f"  sleep 1; done"
	)
	out, _, _ = run_ssh(conn, key, down, timeout_seconds=90)
	if "DOWN" not in out:
		print(f"[{tag}] WARNING: guest never went down after reboot; timing may be off")
	# Now time SSH + serve off the reboot.
	loop = _SERVE_LOOP.format(guest=guest, host=BAKED_SITE, hold=SSH_DEADLINE_SECONDS)
	out, _, _ = run_ssh(conn, key, loop, timeout_seconds=SSH_DEADLINE_SECONDS + 30)
	print(f"[{tag}] {out.strip()}")
	return _parse_serve(out, prefix="reboot_")


def _collect_markers(conn, key, guest) -> dict:
	"""Compact numeric markers for the summary table (mirrors image_boot_benchmark's
	guest profile but read straight, not via the OLD/NEW harness)."""
	payload = r"""
g={guest}
S() {{ ssh -i /tmp/hp.key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o BatchMode=yes -o ConnectTimeout=5 root@"$g" "$1" 2>/dev/null; }}
echo "ANALYZE=$(S 'systemd-analyze 2>/dev/null | head -1')"
echo "RUNNING=$(S 'systemctl list-units --type=service --state=running --no-legend --no-pager 2>/dev/null | wc -l')"
echo "ENABLED=$(S 'systemctl list-unit-files --type=service --state=enabled --no-legend 2>/dev/null | wc -l')"
echo "MASKED=$(S 'systemctl list-unit-files --type=service --state=masked --no-legend 2>/dev/null | wc -l')"
echo "FAILED=$(S 'systemctl --failed --no-legend --no-pager 2>/dev/null | wc -l')"
echo "PSS_KB=$(S 'for d in /proc/[0-9]*; do r=$d/smaps_rollup; [ -r "$r" ] && awk "/^Pss:/{{p+=\$2}} END{{print p+0}}" $r; done | awk "{{s+=\$1}} END{{print s+0}}"')"
echo "PROCS=$(S 'ls -d /proc/[0-9]* 2>/dev/null | wc -l')"
echo "MEMUSED=$(S 'free -m | awk "/^Mem:/{{print \$3}}"')"
echo "PKGS=$(S 'dpkg-query -f "1\n" -W 2>/dev/null | wc -l')"
echo "ROOTUSED=$(S 'df -m --output=used / 2>/dev/null | tail -1 | tr -d " "')"
echo "BUSYJIFF=$(S 'awk "/^cpu /{{print \$2+\$3+\$4+\$7+\$8}}" /proc/stat')"
echo "IOWAITJIFF=$(S 'awk "/^cpu /{{print \$6}}" /proc/stat')"
echo "STEALJIFF=$(S 'awk "/^cpu /{{print \$9}}" /proc/stat')"
echo "MODULES=$(S 'lsmod | tail -n +2 | wc -l')"
echo "PSICPU=$(S 'awk "/some/{{for(i=1;i<=NF;i++)if(\$i~/^total=/){{sub(/total=/,\"\",\$i);print \$i}}}}" /proc/pressure/cpu')"
echo "PSIIO=$(S 'awk "/some/{{for(i=1;i<=NF;i++)if(\$i~/^total=/){{sub(/total=/,\"\",\$i);print \$i}}}}" /proc/pressure/io')"
echo "MANUALPKGS=$(S 'apt-mark showmanual 2>/dev/null | wc -l')"
echo "BUFPOOLMB=$(S 'sock=\$(ls /run/mysqld/*.sock /var/run/mysqld/*.sock 2>/dev/null | head -1); mysql --socket=\$sock -N -B -e "SELECT @@innodb_buffer_pool_size/1048576" 2>/dev/null')"
echo "MARIADBRSS=$(S 'ps -eo rss,comm | awk "/maria|mysqld/{{print int(\$1/1024)}}" | head -1')"
""".format(guest=guest)
	out, _, _ = run_ssh(conn, key, payload, timeout_seconds=60)
	m = {}
	for line in out.splitlines():
		line = line.strip()
		for tag, dst in (
			("RUNNING=", "running_services"),
			("ENABLED=", "enabled_services"),
			("MASKED=", "masked_services"),
			("FAILED=", "failed_units"),
			("PSS_KB=", "pss_kb"),
			("PROCS=", "procs"),
			("MEMUSED=", "mem_used_mb"),
			("PKGS=", "packages"),
			("ROOTUSED=", "root_used_mb"),
			("BUSYJIFF=", "boot_busy_jiffies"),
			("IOWAITJIFF=", "boot_iowait_jiffies"),
			("STEALJIFF=", "boot_steal_jiffies"),
			("MODULES=", "kernel_modules"),
			("PSICPU=", "psi_cpu_total_us"),
			("PSIIO=", "psi_io_total_us"),
			("MANUALPKGS=", "manual_packages"),
			("BUFPOOLMB=", "innodb_buffer_pool_mb"),
			("MARIADBRSS=", "mariadb_rss_mb"),
		):
			if line.startswith(tag):
				v = line[len(tag) :].strip()
				if v.isdigit():
					m[dst] = int(v)
		if line.startswith("ANALYZE="):
			m["analyze"] = line[len("ANALYZE=") :].strip()
	return m


# Host-side: from a common start, record elapsed when TCP:22 opens, when ssh true
# succeeds, and when curl of the baked site returns 'pong'. Single SSH round trip,
# host loop clock authoritative. Prints tagged elapsed markers.
_SERVE_LOOP = r"""
guest={guest}; host={host}
start=$(date +%s.%N); ssh_ok=''; serve=''; ping_ok=''; tcp_ok=''
end=$(( $(date +%s) + {hold} ))
S() {{ ssh -i /tmp/hp.key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o BatchMode=yes -o ConnectTimeout=3 root@"$guest" "$1" 2>/dev/null; }}
while [ $(date +%s) -lt $end ]; do
  now=$(date +%s.%N); el=$(echo "$now - $start" | bc)
  if [ -z "$ping_ok" ] && ping6 -c1 -W1 "$guest" >/dev/null 2>&1; then
    ping_ok=$el; echo "PING_READY=$el"
  fi
  if [ -z "$tcp_ok" ] && timeout 2 bash -c "exec 3<>/dev/tcp/$guest/22" 2>/dev/null; then
    tcp_ok=$el; echo "TCP22_READY=$el"
  fi
  if [ -z "$ssh_ok" ] && timeout 4 ssh -i /tmp/hp.key -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=3 root@"$guest" true 2>/dev/null; then
    ssh_ok=$el; echo "SSH_READY=$el"
  fi
  if [ -n "$ssh_ok" ] && [ -z "$serve" ]; then
    body=$(curl -s -m 8 -H "Host: $host" "http://[$guest]/api/method/ping" 2>/dev/null)
    if echo "$body" | grep -q pong; then
      now=$(date +%s.%N); serve=$(echo "$now - $start" | bc)
      echo "SERVE_READY=$serve"; break
    fi
  fi
  sleep 0.5
done
echo "DONE ssh=$ssh_ok serve=$serve"
"""


def _parse_serve(out: str, prefix: str) -> dict:
	d = {}
	for line in out.splitlines():
		line = line.strip()
		if line.startswith("PING_READY="):
			try:
				d[prefix + "ping_ms"] = float(line.split("=", 1)[1]) * 1000.0
			except ValueError:
				pass
		elif line.startswith("TCP22_READY="):
			try:
				d[prefix + "tcp22_ms"] = float(line.split("=", 1)[1]) * 1000.0
			except ValueError:
				pass
		elif line.startswith("SSH_READY="):
			try:
				d[prefix + "ssh_ms"] = float(line.split("=", 1)[1]) * 1000.0
			except ValueError:
				pass
		elif line.startswith("SERVE_READY="):
			try:
				d[prefix + "serve_ms"] = float(line.split("=", 1)[1]) * 1000.0
			except ValueError:
				pass
	return d


# --- summary ----------------------------------------------------------------

_TIMERS = (
	("ping_ms", "cold ping-ready", "s", 1000.0),
	("tcp22_ms", "cold TCP:22 open", "s", 1000.0),
	("ssh_ms", "cold SSH-ready", "s", 1000.0),
	("serve_ms", "cold serve (pong)", "s", 1000.0),
	("reboot_ping_ms", "reboot ping-ready", "s", 1000.0),
	("reboot_ssh_ms", "reboot SSH-ready", "s", 1000.0),
	("reboot_serve_ms", "reboot serve (pong)", "s", 1000.0),
)
_COUNTS = (
	("analyze", "systemd-analyze", ""),
	("running_services", "running svcs", ""),
	("enabled_services", "enabled svcs", ""),
	("masked_services", "masked svcs", ""),
	("failed_units", "failed units", ""),
	("procs", "procs", ""),
	("packages", "packages", ""),
	("manual_packages", "manual packages", ""),
	("kernel_modules", "kernel modules", ""),
)
_MEM = (
	("pss_kb", "idle PSS", "MB", 1024.0),
	("mem_used_mb", "guest mem used", "MB", 1.0),
	("mariadb_rss_mb", "mariadb RSS", "MB", 1.0),
	("innodb_buffer_pool_mb", "innodb buf pool", "MB", 1.0),
	("root_used_mb", "rootfs used", "MB", 1.0),
	("boot_busy_jiffies", "boot CPU jiffies", "", 1.0),
	("boot_iowait_jiffies", "boot iowait jiffies", "", 1.0),
	("boot_steal_jiffies", "boot steal jiffies", "", 1.0),
	("psi_cpu_total_us", "PSI cpu stall", "ms", 1000.0),
	("psi_io_total_us", "PSI io stall", "ms", 1000.0),
)


def _summary(results: dict) -> None:
	s = results.get("standard")
	m = results.get("minimal")
	print("\n" + "=" * 78)
	print("BENCH IMAGE COMPARISON — golden bench baked on the two optimized bases")
	print("standard = ubuntu-24.04-optimized  |  minimal = ubuntu-24.04-minimal-optimized")
	print("Times off the host loop clock (cold clone + cold reboot); footprint off the")
	print("guest's own /proc & systemd. serve = time to first 200 pong on the baked site.")
	print("=" * 78)

	def g(d, k):
		return d.get(k) if d else None

	print(f"\n{'metric':24s} {'standard':>16s} {'minimal':>16s} {'delta(min-std)':>16s}")
	print("-" * 78)
	for key, label, _unit, div in _TIMERS:
		sv, mv = g(s, key), g(m, key)
		sf = f"{sv / div:.2f}s" if sv is not None else "—"
		mf = f"{mv / div:.2f}s" if mv is not None else "—"
		df = f"{(mv - sv) / div:+.2f}s" if (sv is not None and mv is not None) else "—"
		print(f"{label:24s} {sf:>16s} {mf:>16s} {df:>16s}")
	for key, label, _unit in _COUNTS:
		sv, mv = g(s, key), g(m, key)
		sf = str(sv) if sv is not None else "—"
		mf = str(mv) if mv is not None else "—"
		if isinstance(sv, int) and isinstance(mv, int):
			df = f"{mv - sv:+d}"
		else:
			df = "—"
		print(f"{label:24s} {sf:>16s} {mf:>16s} {df:>16s}")
	for key, label, unit, div in _MEM:
		sv, mv = g(s, key), g(m, key)
		sf = f"{sv / div:.0f}{unit}" if sv is not None else "—"
		mf = f"{mv / div:.0f}{unit}" if mv is not None else "—"
		df = f"{(mv - sv) / div:+.0f}{unit}" if (sv is not None and mv is not None) else "—"
		print(f"{label:24s} {sf:>16s} {mf:>16s} {df:>16s}")
	print("=" * 78)
	for label in ("standard", "minimal"):
		d = results.get(label)
		if d:
			print(f"  {label:9s} vm={d.get('vm')} guest={d.get('guest')} snapshot={d.get('snapshot')}")
