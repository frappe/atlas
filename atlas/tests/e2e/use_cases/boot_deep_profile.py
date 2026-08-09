"""Deep boot-profile use case: provision ONE real Firecracker VM from a given
base image on the live Scaleway host and dump the WHOLE boot story off the
guest's own clocks — kernel timing, systemd critical-chain, every unit's
activation time, blocking points, per-process PSS/RSS/USS, and the guest-vs-host
memory overhead of firecracker+kernel.

This is the diagnostic sibling of `image_boot_benchmark.run` (which only compares
a handful of medians across two images). Here we take one VM and answer:

  * How long did the kernel take, and where in userspace does boot block?
  * What is the systemd critical chain (the serial path that gates the boot)?
  * Which units ran since boot and how long did each take (full blame)?
  * PSS/RSS/USS of every running guest process — what can we delete?
  * Guest RAM in-use vs the firecracker RSS on the host — the true overhead.

Same method trap as the benchmark: reach the guest FROM THE HOST over the
routed-tap path (public v6 is lossy), and read the guest's OWN monotonic clocks.

Run:

    bench --site scaleway.local execute \
      atlas.tests.e2e.use_cases.boot_deep_profile.run --kwargs "{'image':'tier1f'}"

Leaves the VM RUNNING by default so you can poke at it; pass teardown=True to
terminate at the end. Terminate by hand with `_terminate_by_name('<vm>')`.
"""

import base64
import time
import traceback

import frappe

from atlas.atlas.ssh import connection_for_server, run_ssh, ssh_key_file
from atlas.tests.e2e._config import ephemeral_private_key, ephemeral_public_key
from atlas.tests.e2e.use_cases.image_boot_benchmark import (
	_HOST_WAIT,
	SSH_DEADLINE_SECONDS,
	_active_scaleway_server,
	_terminate,
	_wait_for_provision_task,
)

IMAGE = "tier1f"


def run(image: str = IMAGE, server: str = "", teardown: bool = False) -> None:
	server_name = server or _active_scaleway_server()
	conn = connection_for_server(frappe.get_doc("Server", server_name))
	print(f"[deep] server={server_name} image={image} teardown={teardown}")

	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": f"deep-profile {image}",
			"server": server_name,
			"image": image,
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 4,
			"ssh_public_key": ephemeral_public_key(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	vm_name = vm.name
	print(f"[deep] VM {vm_name} inserted; waiting for provision Task…")

	try:
		task = _wait_for_provision_task(vm_name, timeout_seconds=180)
		provision_ms = (task["ended"] - task["started"]).total_seconds() * 1000.0
		vm.reload()
		guest = vm.ipv6_address
		print(f"[deep] provision={provision_ms / 1000:.2f}s guest={guest}")

		with ssh_key_file(conn.ssh_private_key) as key:
			_stage_probe_key(conn, key)
			# Block until the guest answers ssh.
			out, _, _ = run_ssh(
				conn,
				key,
				_HOST_WAIT.format(guest=guest, hold=SSH_DEADLINE_SECONDS),
				timeout_seconds=SSH_DEADLINE_SECONDS + 30,
			)
			if "SSH_READY" not in out:
				print(f"[deep] guest {guest} never answered SSH host-side; aborting dump")
				return

			print("\n" + "#" * 78)
			print(f"# DEEP BOOT PROFILE — image={image} vm={vm_name} guest={guest}")
			print("#" * 78)

			_dump_analyze(conn, key, guest)
			_dump_critical_chain(conn, key, guest)
			_dump_blame(conn, key, guest)
			_dump_kernel_timeline(conn, key, guest)
			_dump_units(conn, key, guest)
			_dump_processes(conn, key, guest)
			_dump_cpu(conn, key, guest)
			_dump_boot_bound(conn, key, guest)
			_dump_pressure(conn, key, guest)
			_dump_memory(conn, key, guest)
			_dump_cgroup_memory(conn, key, guest)
			_dump_mariadb(conn, key, guest)
			_dump_modules(conn, key, guest)
			_dump_disk(conn, key, guest)
			_dump_net_ssh(conn, key, guest)
			_dump_packages(conn, key, guest)
			_dump_package_classes(conn, key, guest)
			_dump_host_overhead(conn, key, vm_name, guest)
	except Exception:
		traceback.print_exc()
	finally:
		if teardown:
			_terminate(vm_name)
			print(f"[deep] terminated {vm_name}")
		else:
			print(f"\n[deep] VM {vm_name} LEFT RUNNING — _terminate_by_name('{vm_name}')")


def _terminate_by_name(vm_name: str) -> None:
	_terminate(vm_name)
	print(f"[deep] terminated {vm_name}")


# ---------------------------------------------------------------------------
# Guest command helper: build a host->guest ssh one-liner. `payload` runs on the
# guest. We base64 the payload so arbitrary quoting/newlines survive two hops.
# ---------------------------------------------------------------------------


def _guest(conn, key: str, guest: str, payload: str, timeout: int = 60) -> str:
	b64 = base64.b64encode(payload.encode()).decode()
	cmd = (
		f"echo {b64} | base64 -d | "
		f"ssh -i /tmp/hp.key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
		f'-o BatchMode=yes -o ConnectTimeout=6 root@{guest} "bash -s" 2>/dev/null'
	)
	out, _, _ = run_ssh(conn, key, cmd, timeout_seconds=timeout)
	return out


def _stage_probe_key(conn, key: str) -> None:
	priv_b64 = base64.b64encode(ephemeral_private_key().encode()).decode()
	run_ssh(
		conn,
		key,
		f"echo {priv_b64} | base64 -d > /tmp/hp.key && chmod 600 /tmp/hp.key",
		timeout_seconds=20,
	)


# ---------------------------------------------------------------------------
# Individual dumps
# ---------------------------------------------------------------------------


def _section(title: str) -> None:
	print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def _dump_analyze(conn, key, guest):
	_section("systemd-analyze (kernel / userspace / total to default target)")
	print(_guest(conn, key, guest, "systemd-analyze; echo; systemd-analyze time 2>/dev/null || true"))


def _dump_critical_chain(conn, key, guest):
	_section("systemd-analyze critical-chain (the SERIAL path that gates boot)")
	print(_guest(conn, key, guest, "systemd-analyze critical-chain --no-pager 2>/dev/null"))


def _dump_blame(conn, key, guest):
	_section("systemd-analyze blame (per-unit activation time, slowest first)")
	print(_guest(conn, key, guest, "systemd-analyze blame --no-pager 2>/dev/null | head -40"))


def _dump_kernel_timeline(conn, key, guest):
	_section("Kernel timeline — dmesg milestones (init/rootfs/random/net/systemd)")
	# Pull the load-bearing kernel timestamps: last kernel line before init hand-off,
	# rootfs mount, crng, virtio bring-up, and the systemd start marker.
	payload = r"""
dmesg 2>/dev/null | grep -iE \
  'crng init|random: |Run /sbin/init|systemd\[1\]|EXT4-fs .*mounted|virtio|Freeing unused kernel|Command line|clocksource|smp:|Booting|Linux version' \
  | head -50
echo '--- last kernel timestamp (init handoff) ---'
dmesg 2>/dev/null | tail -1
"""
	print(_guest(conn, key, guest, payload))


def _dump_units(conn, key, guest):
	_section("All units — running + failed, and count by state")
	payload = r"""
echo '--- failed units ---'
systemctl --failed --no-legend --no-pager 2>/dev/null || echo '(none)'
echo '--- running services (active) ---'
systemctl list-units --type=service --state=running --no-legend --no-pager 2>/dev/null
echo '--- state summary ---'
systemctl list-units --all --no-legend --no-pager 2>/dev/null | awk '{print $3}' | sort | uniq -c | sort -rn
echo '--- enabled/masked service count ---'
echo -n 'enabled services: '; systemctl list-unit-files --type=service --state=enabled --no-legend 2>/dev/null | wc -l
echo -n 'masked services:  '; systemctl list-unit-files --type=service --state=masked --no-legend 2>/dev/null | wc -l
"""
	print(_guest(conn, key, guest, payload))


def _dump_processes(conn, key, guest):
	_section("Per-process memory PSS/RSS/USS (from /proc/*/smaps_rollup, KB)")
	# smaps_rollup gives Pss/Rss/Private (USS ~= Private_Clean+Private_Dirty).
	# Sum per pid, join with comm, sort by PSS. Pure /proc, no smem dependency.
	payload = r"""
# Emit pid|comm|pss|rss|uss per process to a temp file (no subshell so totals survive).
tmp=$(mktemp)
for d in /proc/[0-9]*; do
  pid=${d#/proc/}
  roll=$d/smaps_rollup
  [ -r "$roll" ] || continue
  comm=$(tr -d '\0' < $d/comm 2>/dev/null)
  read pss rss uss < <(awk '/^Pss:/{p+=$2} /^Rss:/{r+=$2} /^Private_Clean:|^Private_Dirty:/{u+=$2} END{print p+0, r+0, u+0}' $roll)
  echo "$pid|$comm|$pss|$rss|$uss" >> $tmp
done
printf '%-8s %-22s %10s %10s %10s\n' PID COMM PSS_KB RSS_KB USS_KB
sort -t'|' -k3 -rn $tmp | head -30 | awk -F'|' '{printf "%-8s %-22s %10d %10d %10d\n",$1,$2,$3,$4,$5}'
echo "---"
awk -F'|' '{p+=$3; r+=$4; u+=$5} END{printf "TOTAL userspace: PSS=%dKB (%.1fMB)  RSS=%dKB  USS=%dKB (%.1fMB)\n", p, p/1024, r, u, u/1024}' $tmp
echo "userspace process count: $(wc -l < $tmp)"
echo "total procs (incl kthreads): $(ls -d /proc/[0-9]* | wc -l)"
rm -f $tmp
"""
	print(_guest(conn, key, guest, payload))


def _dump_cpu(conn, key, guest):
	_section("CPU — load, idle %, and cumulative busy time since boot")
	# On an idle post-boot VM the interesting number is how much CPU boot BURNED
	# (cumulative user+system jiffies) and whether anything is still spinning.
	# We read /proc/stat twice 2s apart for a live idle%, plus uptime/loadavg.
	payload = r"""
echo '--- uptime / loadavg ---'
uptime
cat /proc/loadavg
echo '--- cumulative CPU since boot (/proc/stat, USER_HZ jiffies) ---'
awk '/^cpu /{printf "user=%d nice=%d system=%d idle=%d iowait=%d irq=%d softirq=%d\n",$2,$3,$4,$5,$6,$7,$8}' /proc/stat
busy=$(awk '/^cpu /{print $2+$3+$4+$7+$8}' /proc/stat)
idle=$(awk '/^cpu /{print $5+$6}' /proc/stat)
echo "cumulative busy jiffies=$busy idle jiffies=$idle  (=> boot burned ~$((busy)) jiffies of CPU)"
echo '--- live idle% over 2s ---'
read a b < <(awk '/^cpu /{print $2+$3+$4+$6+$7+$8, $2+$3+$4+$5+$6+$7+$8}' /proc/stat)
sleep 2
read c d < <(awk '/^cpu /{print $2+$3+$4+$6+$7+$8, $2+$3+$4+$5+$6+$7+$8}' /proc/stat)
awk -v a=$a -v b=$b -v c=$c -v d=$d 'BEGIN{db=d-b; if(db>0) printf "busy=%.1f%% idle=%.1f%%\n",100*(c-a)/db,100*(1-(c-a)/db); else print "n/a"}'
echo '--- top 5 CPU consumers right now ---'
ps -eo pid,comm,%cpu,time --sort=-%cpu 2>/dev/null | head -6
"""
	print(_guest(conn, key, guest, payload))


def _dump_memory(conn, key, guest):
	_section("Guest memory — /proc/meminfo highlights + free")
	payload = r"""
free -m
echo '--- meminfo ---'
grep -E 'MemTotal|MemFree|MemAvailable|Buffers|Cached|Slab|KReclaimable|SReclaimable|SUnreclaim|PageTables|KernelStack|Committed_AS' /proc/meminfo
echo '--- kernel memory (guest side) ---'
grep -E 'Slab|VmallocUsed|Percpu' /proc/meminfo
"""
	print(_guest(conn, key, guest, payload))


def _dump_disk(conn, key, guest):
	_section("Disk — filesystem usage, rootfs size, ZFS if present")
	payload = r"""
echo '--- df (real filesystems) ---'
df -hT -x tmpfs -x devtmpfs 2>/dev/null
echo '--- lsblk ---'
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT 2>/dev/null
echo '--- ZFS pools/datasets (if any) ---'
if command -v zpool >/dev/null 2>&1; then
  zpool list 2>/dev/null || echo '(no pool)'
  zfs list 2>/dev/null | head -20 || true
else
  echo '(zfs userspace not present)'
fi
echo '--- root fs used bytes (du of / one level) ---'
du -xhd1 / 2>/dev/null | sort -rh | head -15
echo '--- /var breakdown (the usual rootfs hog: apt cache, logs, mysql, journal) ---'
du -xhd2 /var 2>/dev/null | sort -rh | head -20
echo '--- biggest single dirs anywhere on rootfs (top 15) ---'
du -xhd3 / 2>/dev/null | sort -rh | head -15
echo '--- apt archive + journal + log sizes (reclaimable) ---'
du -sh /var/cache/apt/archives /var/log /var/lib/apt/lists 2>/dev/null
journalctl --disk-usage 2>/dev/null
"""
	print(_guest(conn, key, guest, payload))


def _dump_packages(conn, key, guest):
	_section("Installed packages — dpkg count + full manifest (sorted)")
	payload = r"""
echo -n 'installed package count: '
dpkg-query -f '${binary:Package}\n' -W 2>/dev/null | wc -l
echo '--- top 20 by installed-size (KB) ---'
dpkg-query -f '${Installed-Size}\t${binary:Package}\n' -W 2>/dev/null | sort -rn | head -20
echo '--- FULL package list (name version) ---'
dpkg-query -f '${binary:Package}\t${Version}\n' -W 2>/dev/null | sort
"""
	print(_guest(conn, key, guest, payload))


def _dump_boot_bound(conn, key, guest):
	_section("Boot bound — was boot CPU-bound, IO-bound, or throttled? (iowait vs busy)")
	# The single most useful "what's the blocker" number: split the boot's cumulative
	# jiffies into user+system (compute) vs iowait (disk/blk stall). A boot that is
	# mostly iowait is disk-bound; mostly system is compute-bound; steal>0 means the
	# host is throttling this vCPU. cpu.max on the guest's own root cgroup shows the
	# cgroup cap (the throttle we deliberately apply on tiny bare-boot VMs).
	payload = r"""
awk '/^cpu /{u=$2;n=$3;s=$4;io=$6;irq=$7;sirq=$8;st=$9; tot=u+n+s+io+irq+sirq+st;
  if(tot==0)tot=1;
  printf "compute(user+sys+nice)=%d (%.1f%%)  iowait=%d (%.1f%%)  irq+softirq=%d  steal=%d (%.1f%%)\n",
    u+n+s,100*(u+n+s)/tot, io,100*io/tot, irq+sirq, st,100*st/tot}' /proc/stat
echo '--- steal (host throttling this vCPU): nonzero => host oversubscribed/capped ---'
grep -E '^cpu[0-9]' /proc/stat | awk '{print $1" steal="$9}'
echo '--- guest root cgroup cpu cap (cpu.max: "<quota> <period>", max=uncapped) ---'
cat /sys/fs/cgroup/cpu.max 2>/dev/null || echo '(no cgroup2 cpu.max)'
echo '--- per-slice cpu.stat (throttled time if capped) ---'
for s in system.slice user.slice init.scope; do
  f=/sys/fs/cgroup/$s/cpu.stat
  [ -r "$f" ] && { echo "== $s =="; grep -E 'nr_throttled|throttled_usec|usage_usec' $f; }
done
echo '--- pressure-derived: was anything STALLED waiting on IO during boot? ---'
cat /proc/pressure/io 2>/dev/null
"""
	print(_guest(conn, key, guest, payload))


def _dump_pressure(conn, key, guest):
	_section("PSI pressure — CPU / IO / MEMORY stall (the definitive 'what are we bound on')")
	# /proc/pressure is the kernel's own verdict on resource contention. `some avg`
	# = share of time at least one task stalled on that resource; `full` = everyone
	# stalled. High io.some at idle => disk-bound; high cpu.some => CPU-bound; any
	# memory.full => memory-bound (reclaim thrash). This directly answers the
	# "CPU/Memory/Disk-IO/Network bound?" question with a kernel-authoritative number.
	payload = r"""
for r in cpu io memory; do
  echo "== /proc/pressure/$r =="
  cat /proc/pressure/$r 2>/dev/null || echo '(PSI not available)'
done
echo '--- per-slice memory.pressure (which cgroup is starved) ---'
for s in system.slice user.slice; do
  f=/sys/fs/cgroup/$s/memory.pressure
  [ -r "$f" ] && { echo "== $s =="; cat $f; }
done
"""
	print(_guest(conn, key, guest, payload))


def _dump_cgroup_memory(conn, key, guest):
	_section("Memory by cgroup slice/scope (where the RAM actually lives, by purpose)")
	# current.memory per systemd slice/scope answers "how is memory distributed by
	# group/purpose" far better than per-proc PSS: MariaDB, redis, nginx, gunicorn
	# each live in their own user@ scope. Walk cgroup2 memory.current and label.
	payload = r"""
root=/sys/fs/cgroup
printf '%-52s %10s\n' CGROUP MEM_MB
find $root -name memory.current 2>/dev/null | while read f; do
  d=$(dirname $f); cur=$(cat $f 2>/dev/null)
  [ "${cur:-0}" -gt 5242880 ] || continue    # skip <5MB noise
  name=${d#$root/}; [ -z "$name" ] && name='(root)'
  printf '%-52s %10.1f\n' "$name" "$(awk -v c=$cur 'BEGIN{print c/1048576}')"
done | sort -k2 -rn | head -25
echo '--- anon vs file vs kernel split (root memory.stat) ---'
grep -E '^anon |^file |^kernel |^slab |^sock |^kernel_stack |^pagetables ' $root/memory.stat 2>/dev/null
"""
	print(_guest(conn, key, guest, payload))


def _dump_mariadb(conn, key, guest):
	_section("MariaDB memory — buffer pool, engine footprint (the biggest single consumer)")
	# MariaDB is the boot critical-chain gate AND the largest RSS. Pull the configured
	# innodb_buffer_pool_size (what we'd shrink to lower the memory floor), the actual
	# RSS of the mariadbd process, and the effective my.cnf so the floor sweep knows
	# its starting point. Socket path is bench-cli's dedicated `atlas` instance.
	payload = r"""
sock=$(ls /var/run/mysqld/*.sock /run/mysqld/*.sock 2>/dev/null | head -1)
echo "socket=$sock"
# Try to read runtime InnoDB vars via the unix socket as the bench sql user, else via config.
mysql_cmd() { mysql --socket="$sock" -N -B 2>/dev/null -e "$1"; }
echo '--- runtime InnoDB memory vars (if reachable) ---'
mysql_cmd "SELECT @@innodb_buffer_pool_size/1048576 AS buf_pool_mb, @@innodb_buffer_pool_instances AS instances, @@innodb_log_file_size/1048576 AS log_mb, @@max_connections AS maxconn, @@key_buffer_size/1048576 AS key_buf_mb;" || echo '(socket auth unavailable — reading config instead)'
echo '--- configured innodb/buffer settings in my.cnf tree ---'
grep -rniE 'innodb_buffer_pool_size|innodb_buffer_pool_instances|innodb_log_file_size|innodb_log_buffer|key_buffer_size|max_connections|performance_schema|table_open_cache|tmp_table_size' /etc/mysql /etc/my.cnf* 2>/dev/null | grep -vE '^\s*#' | head -30
echo '--- mariadbd process RSS ---'
ps -eo pid,rss,comm 2>/dev/null | awk '/maria|mysqld/{printf "pid=%s rss=%.0fMB comm=%s\n",$1,$2/1024,$3}'
echo '--- mariadbd cgroup memory.current ---'
mp=$(pgrep -x mariadbd || pgrep -x mysqld); [ -n "$mp" ] && {
  cg=$(awk -F: '/^0::/{print $3}' /proc/$mp/cgroup)
  cur=/sys/fs/cgroup$cg/memory.current
  [ -r "$cur" ] && printf 'mariadb scope memory.current=%.0fMB\n' "$(awk -v c=$(cat $cur) 'BEGIN{print c/1048576}')"
}
"""
	print(_guest(conn, key, guest, payload))


def _dump_modules(conn, key, guest):
	_section("Kernel modules — what's loaded, sizes, and what we could strip")
	# Answers "is boot/net/memory cost coming from modules; can we disable them; what
	# modules are we running". lsmod with sizes, sorted; flag the ones we know are
	# load-bearing (virtio_*, zfs, spl) vs candidates. Also kernel image size + the
	# builtin-vs-modular split (only modular ones can be stripped without a rebuild).
	payload = r"""
echo '--- loaded modules (size KB, refcount), largest first ---'
lsmod | tail -n +2 | awk '{printf "%-24s %10.1fKB refs=%s deps=%s\n",$1,$2/1024,$3,$4}' | sort -k2 -rn
echo -n 'module count: '; lsmod | tail -n +2 | wc -l
echo -n 'total module memory: '; lsmod | tail -n +2 | awk '{s+=$2} END{printf "%.1fMB\n",s/1048576}'
echo '--- ZFS/SPL footprint (largest by design) ---'
lsmod | awk '/^zfs|^spl|^zlua|^zzstd|^zcommon|^znvpair|^zavl|^zunicode|^icp/{s+=$2; print $1" "$2/1024"KB"} END{printf "zfs-family total: %.1fMB\n",s/1048576}'
echo '--- kernel image + modules-on-disk size ---'
ls -la /boot/vmlinuz* 2>/dev/null | awk '{print $5" "$9}'
du -sh /lib/modules/$(uname -r) 2>/dev/null
echo '--- modules-load.d pins (what we force-load at boot) ---'
cat /etc/modules-load.d/*.conf 2>/dev/null | grep -vE '^\s*#|^\s*$'
echo '--- kernel version / cmdline ---'
uname -r; cat /proc/cmdline
"""
	print(_guest(conn, key, guest, payload))


def _dump_net_ssh(conn, key, guest):
	_section("Network + SSH readiness — what gates first-connect (why SSH/ping 'slow')")
	# The "slow SSH/network" question: SSH readiness is gated by (a) the network being
	# up (link + v6 addr + route) and (b) the ssh LISTENER being available. CAVEAT:
	# Ubuntu ships ssh SOCKET-ACTIVATED — ssh.socket binds :22 early (watch its
	# ActiveEnterTimestamp, ~1s), and ssh.service (the sshd process) only spawns on
	# the FIRST connection. So sshd's "Server listening" journal line is when the
	# host first CONNECTED, not when SSH became reachable — do NOT read it as a delay.
	# The true "SSH reachable" moment is max(network-online, ssh.socket active). A
	# bench VM adds no net units over base — if first-connect is later on bench, it's
	# boot-time CPU contention (baking workers/MariaDB), not networking.
	payload = r"""
echo '--- when did network-online / ssh.socket / ssh.service become active (monotonic) ---'
echo '    (ssh.socket = TRUE reachable moment; ssh.service = first-connect, NOT a delay)'
for u in systemd-networkd.service systemd-networkd-wait-online.service network-online.target ssh.socket ssh.service sshd.service; do
  t=$(systemctl show -p ActiveEnterTimestampMonotonic --value $u 2>/dev/null)
  [ -n "$t" ] && [ "$t" != "0" ] && printf '%-40s +%.2fs\n' "$u" "$(awk -v t=$t 'BEGIN{print t/1e6}')"
done
echo '--- sshd first-listen from journal (= first CONNECT under socket activation) ---'
journalctl -u ssh -u sshd --no-pager -o short-monotonic 2>/dev/null | grep -iE 'listening|server listening|Server listening' | head -3
echo '--- link + addr + route (is v6 up, any DAD/tentative delay) ---'
ip -6 addr show scope global 2>/dev/null | grep -E 'inet6|tentative|dadfailed'
ip -6 route show default 2>/dev/null
echo '--- net device counters (drops/errors would explain slow) ---'
ip -s link show 2>/dev/null | grep -A2 -E '^[0-9]+: (eth|ens|enp)' | head -12
echo '--- sshd config knobs that slow first connect (DNS, GSSAPI) ---'
grep -iE '^UseDNS|^GSSAPIAuthentication|^UsePAM' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null
"""
	print(_guest(conn, key, guest, payload))


def _dump_package_classes(conn, key, guest):
	_section("Package classification — manual vs auto, priority, recommends (the cruft map)")
	# Answers "extra/missing packages: needed, recommended, maybe, useless; can we
	# strip more". apt-mark manual = explicitly requested (our + bench's real deps);
	# auto = pulled as deps (safe to autoremove if nothing needs them). Priority
	# required/important = distro-essential (keep); standard/optional pulled by
	# install-recommends are the strip candidates. autoremove --dry-run names the
	# provably-dead ones. Cross-check what's installed purely as a Recommends.
	payload = r"""
echo -n 'total installed: '; dpkg-query -f '.\n' -W 2>/dev/null | wc -l
echo -n 'manually installed (apt-mark manual): '; apt-mark showmanual 2>/dev/null | wc -l
echo -n 'auto (dependency) installed: '; apt-mark showauto 2>/dev/null | wc -l
echo '--- by Priority (required/important = distro-essential; optional = strip candidates) ---'
dpkg-query -f '${Priority}\n' -W 2>/dev/null | sort | uniq -c | sort -rn
echo '--- MANUALLY installed packages (our + bench real deps — the intentional set) ---'
apt-mark showmanual 2>/dev/null | sort
echo '--- provably removable RIGHT NOW (apt autoremove --dry-run) ---'
DEBIAN_FRONTEND=noninteractive apt-get -s autoremove 2>/dev/null | grep -E '^Remv|packages will be REMOVED|to remove' | head -40
echo '--- top 25 optional-priority packages by size (recommend-pulled cruft candidates) ---'
while read sz pkg; do
  pri=$(dpkg-query -f '${Priority}' -W "$pkg" 2>/dev/null)
  [ "$pri" = optional ] || [ "$pri" = extra ] && printf '%8s KB  %-30s %s\n' "$sz" "$pkg" "$pri"
done < <(dpkg-query -f '${Installed-Size}\t${binary:Package}\n' -W 2>/dev/null | sort -rn | head -60) | head -25
"""
	print(_guest(conn, key, guest, payload))


def _dump_host_overhead(conn, key, vm_name, guest):
	_section("Host-side firecracker overhead (guest RAM vs firecracker RSS on host)")
	# Find the firecracker/jailer process for this VM on the HOST and read its
	# RSS/PSS from the host's /proc. Compare to the guest's configured 512MB and
	# actual in-use. This is the "what does the VM cost the host" number.
	payload = f"""
set +e
echo '--- firecracker process for {vm_name} on host ---'
pid=$(pgrep -af firecracker | grep {vm_name} | awk '{{print $1}}' | head -1)
if [ -z "$pid" ]; then
  # jailer renames; match by the vm dir instead
  pid=$(sudo pgrep -af 'firecracker|jailer' | grep -i {vm_name[:12]} | awk '{{print $1}}' | head -1)
fi
echo "fc pid=$pid"
if [ -n "$pid" ]; then
  echo '--- host /proc smaps_rollup for fc ---'
  sudo cat /proc/$pid/smaps_rollup 2>/dev/null | grep -E '^Rss:|^Pss:|^Private'
  echo '--- host status VmRSS/VmHWM ---'
  sudo grep -E 'VmRSS|VmHWM|VmSize' /proc/$pid/status 2>/dev/null
fi
echo '--- host free ---'
free -m | head -2
"""
	out, err, _ = run_ssh(conn, key, payload, timeout_seconds=45)
	print(out)
	if err.strip():
		print(f"ERR: {err[-300:]}")
