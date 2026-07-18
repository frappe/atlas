"""Use case: compare boot / network-ready / SSH-ready times of VMs booted from
two base images on a live Scaleway host.

Motivation: we baked a virtio_rng driver into the guest image
(`ubuntu-24.04-entropy3`) so the guest's CSPRNG seeds from the host's hardware
RNG at boot instead of blocking on entropy. sshd host-key generation and the
first TLS/SSH handshake are the classic entropy-starved boot steps, so the
hypothesis is that the entropy image reaches "SSH accepts a command" sooner (or
at least no slower) than the plain `ubuntu-24.04` server image.

This provisions REAL Firecracker VMs on the Active Scaleway box (billable, but
tiny 1-vCPU/512MB/4GB VMs, torn down immediately), N times per image, and reads
each guest's boot profile off the guest's OWN clocks once it answers SSH:

  * provision_ms  — insert -> provision-vm Task Success (disk prep + unit start).
                    Context only; varies with pool/LV warmth, not the image.
  * kernel_ms     — systemd-analyze kernel phase (firmware/loader excluded on FC).
  * userspace_ms  — systemd-analyze userspace phase — where a blocking first-boot
                    unit (pollinate) or slow service shows up.
  * total_ms      — systemd-analyze total to the default target.
  * sshd_ms       — ssh.service ActiveEnterTimestampMonotonic: boot -> sshd
                    serving. The "when can I log in?" number.
  * crng_ms       — kernel 'crng init done' (dmesg): when the CSPRNG was seeded.
                    The direct entropy marker virtio_rng was meant to move.
  * hwrng         — /sys/.../rng_current: the bound hardware-RNG (virtio_rng.N if
                    the baked driver took, 'none' otherwise). Categorical.

CRITICAL METHOD NOTE: the guest is reached FROM THE SCALEWAY HOST, not from this
controller. The controller (a laptop) reaches the guest /128 only over the
public-internet v6 path, whose packet loss (seconds of jitter, sometimes total
outage) makes wall-clock timing from here worthless. The host has a direct,
lossless routed-tap route to every guest. And because the guest usually finishes
booting DURING the multi-second provision, we don't time from the host at all —
we read the guest's own systemd/dmesg monotonic clocks, which carry zero
controller/host offset and isolate the image's boot cost exactly.

Run:

    bench --site scaleway.local execute \
      atlas.tests.e2e.use_cases.image_boot_benchmark.run

Optional kwargs: old_image, new_image, runs (default 3), server (auto-detected
Active Scaleway box if omitted).
"""

import re
import statistics
import time
import traceback

import frappe

from atlas.atlas.ssh import connection_for_server, run_ssh, ssh_key_file
from atlas.tests.e2e._config import ephemeral_private_key, ephemeral_public_key

OLD_IMAGE = "ubuntu-24.04"
NEW_IMAGE = "ubuntu-24.04-entropy3"
NETWORK_DEADLINE_SECONDS = 180
SSH_DEADLINE_SECONDS = 240


def run(
	old_image: str = OLD_IMAGE,
	new_image: str = NEW_IMAGE,
	runs: int = 3,
	server: str = "",
) -> None:
	server_name = server or _active_scaleway_server()
	print(f"[bench] server={server_name} old={old_image} new={new_image} runs={runs}")

	# Interleave OLD/NEW so any slow drift in the host (thermals, pool fill)
	# hits both images evenly rather than loading all of one image's runs into
	# one time window.
	order = []
	for i in range(runs):
		order.append((old_image, i))
		order.append((new_image, i))

	results: dict[str, list[dict]] = {old_image: [], new_image: []}
	for image, i in order:
		label = "OLD" if image == old_image else "NEW"
		print(f"\n[bench] === {label} {image} run {i + 1}/{runs} ===")
		try:
			sample = _measure_one(server_name, image)
			results[image].append(sample)
			print(f"[bench] {label} run {i + 1}: {_fmt(sample)}")
		except Exception:
			print(f"[bench] {label} run {i + 1}: FAILED")
			traceback.print_exc()

	_report(old_image, new_image, results)


def _diag_from_host(guest_v6: str, server: str = "") -> None:
	"""Probe a guest /128 FROM THE HOST (routed-tap path, no public v6). Prints
	ping + TCP :22 + SSH banner as seen from the host."""
	server_name = server or _active_scaleway_server()
	conn = connection_for_server(frappe.get_doc("Server", server_name))
	cmd = (
		f"ping6 -c 3 -W 2 {guest_v6} 2>&1 | tail -3; echo '=== TCP :22 ==='; "
		f"timeout 5 bash -c 'exec 3<>/dev/tcp/{guest_v6}/22' && echo TCP_OPEN || echo TCP_CLOSED; "
		f"echo '=== banner ==='; timeout 8 bash -c 'exec 3<>/dev/tcp/{guest_v6}/22; head -c 40 <&3' 2>&1"
	)
	with ssh_key_file(conn.ssh_private_key) as key:
		out, err, rc = run_ssh(conn, key, cmd, timeout_seconds=45)
	print(f"[diag-host] rc={rc}\n{out}\nERR:{err[-300:] if err else ''}")


def _diag_vm_health(vm_name: str, server: str = "") -> None:
	"""Inspect a VM's host-side liveness: systemd unit state, firecracker PID,
	tap device, and the guest's console log tail. Tells us whether the guest is
	alive-but-unreachable vs dead."""
	server_name = server or _active_scaleway_server()
	conn = connection_for_server(frappe.get_doc("Server", server_name))
	unit = f"firecracker-vm@{vm_name}.service"
	tap = "atlas-" + vm_name.replace("-", "")[:9]
	cmd = (
		f"echo '=== unit ==='; systemctl is-active {unit}; "
		f"systemctl show {unit} -p ActiveState,SubState,NRestarts,ExecMainStatus --value | tr '\\n' ' '; echo; "
		f"echo '=== fc proc ==='; pgrep -af firecracker | grep {vm_name} || echo 'no fc proc'; "
		f"echo '=== tap ==='; ip -br link show {tap} 2>&1 || echo 'no tap'; "
		f"echo '=== journal tail ==='; journalctl -u {unit} -n 25 --no-pager 2>&1 | tail -25; "
		f"echo '=== fc log tail ==='; sudo tail -20 /var/lib/atlas/virtual-machines/{vm_name}/logs/*.log 2>&1 | tail -20"
	)
	with ssh_key_file(conn.ssh_private_key) as key:
		out, err, rc = run_ssh(conn, key, cmd, timeout_seconds=45)
	print(f"[vm-health] rc={rc}\n{out}\nERR:{err[-500:] if err else ''}")


def probe_from_host(image: str = OLD_IMAGE, server: str = "", hold_seconds: int = 60) -> None:
	"""Provision ONE VM, then repeatedly probe it FROM THE HOST (routed-tap, not
	public v6) for `hold_seconds`, printing when TCP:22 opens and when an SSH
	`true` succeeds. Leaves the VM up at the end (no terminate) for inspection —
	proves whether host-side timing is the reliable measurement path before we
	rebuild the harness around it."""
	server_name = server or _active_scaleway_server()
	conn = connection_for_server(frappe.get_doc("Server", server_name))

	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": f"host-probe {image}",
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
	print(f"[host-probe] VM {vm_name} inserted, waiting for provision Task…")

	task = _wait_for_provision_task(vm_name, timeout_seconds=180)
	print(
		f"[host-probe] provision Task {task['name']} done in "
		f"{(task['ended'] - task['started']).total_seconds():.1f}s; probing from host"
	)
	vm.reload()
	guest = vm.ipv6_address
	print(f"[host-probe] guest v6 = {guest}")

	# One SSH session to the host that loops probing the guest, printing an
	# elapsed marker each time a milestone flips. Keeps to a single SSH round
	# trip so the host's own loop clock is authoritative (no per-probe SSH cost).
	loop = (
		f"guest={guest}; start=$(date +%s.%N); tcp=''; ssh_ok=''; "
		f"end=$(( $(date +%s) + {hold_seconds} )); "
		f"while [ $(date +%s) -lt $end ]; do "
		f'  now=$(date +%s.%N); el=$(echo "$now - $start" | bc); '
		f'  if [ -z "$tcp" ] && timeout 2 bash -c "exec 3<>/dev/tcp/$guest/22" 2>/dev/null; then '
		f'    tcp=$el; echo "TCP_OPEN at ${{el}}s"; fi; '
		f'  if [ -z "$ssh_ok" ] && timeout 4 ssh -i /tmp/hp.key -o StrictHostKeyChecking=no '
		f"-o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=3 root@$guest true 2>/dev/null; then "
		f'    ssh_ok=$el; echo "SSH_READY at ${{el}}s"; break; fi; '
		f"  sleep 0.5; "
		f'done; echo "DONE tcp=$tcp ssh=$ssh_ok"'
	)
	with ssh_key_file(conn.ssh_private_key) as key:
		# Stage the guest probe key on the host first.
		import base64

		priv_b64 = base64.b64encode(ephemeral_private_key().encode()).decode()
		run_ssh(
			conn,
			key,
			f"echo {priv_b64} | base64 -d > /tmp/hp.key && chmod 600 /tmp/hp.key",
			timeout_seconds=20,
		)
		out, err, rc = run_ssh(conn, key, loop, timeout_seconds=hold_seconds + 30)
	print(f"[host-probe] rc={rc}\n{out}\nERR:{err[-300:] if err else ''}")
	print(f"[host-probe] VM {vm_name} LEFT RUNNING — terminate with _terminate_by_name('{vm_name}')")


def _terminate_by_name(vm_name: str) -> None:
	_terminate(vm_name)
	print(f"[host-probe] terminated {vm_name}")


def probe_guest_boot_detail(image: str = NEW_IMAGE, server: str = "") -> None:
	"""Provision one VM, wait host-side for SSH, then dump the guest's full boot
	profile so we can see which markers are worth capturing. Leaves the VM up."""
	server_name = server or _active_scaleway_server()
	conn = connection_for_server(frappe.get_doc("Server", server_name))
	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": f"boot-detail {image}",
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
	_wait_for_provision_task(vm_name, timeout_seconds=180)
	vm.reload()
	guest = vm.ipv6_address
	print(f"[detail] VM {vm_name} guest={guest}")
	with ssh_key_file(conn.ssh_private_key) as key:
		_stage_probe_key(conn, key)
		# Wait for ssh-ready via the host loop, then dump everything.
		run_ssh(
			conn,
			key,
			_HOST_WAIT.format(guest=guest, hold=SSH_DEADLINE_SECONDS),
			timeout_seconds=SSH_DEADLINE_SECONDS + 30,
		)
		dump = (
			"set +e; g=" + guest + "; "
			"S() { ssh -i /tmp/hp.key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
			'-o BatchMode=yes -o ConnectTimeout=5 root@$g "$1"; }; '
			"echo '=== systemd-analyze ==='; S 'systemd-analyze 2>/dev/null'; "
			"echo '=== ssh.service active-monotonic(us) ==='; S 'systemctl show ssh.service -p ActiveEnterTimestampMonotonic --value'; "
			"echo '=== crng init (dmesg) ==='; S 'dmesg 2>/dev/null | grep -i \"crng init\\|random:\" | head'; "
			"echo '=== hwrng ==='; S 'cat /sys/class/misc/hw_random/rng_current 2>/dev/null; lsmod | grep virtio_rng'; "
			"echo '=== blame top5 ==='; S 'systemd-analyze blame 2>/dev/null | head -5'"
		)
		out, err, rc = run_ssh(conn, key, dump, timeout_seconds=60)
	print(f"[detail] rc={rc}\n{out}\nERR:{err[-300:] if err else ''}")
	print(f"[detail] VM {vm_name} LEFT RUNNING — _terminate_by_name('{vm_name}')")


def smoke(image: str = OLD_IMAGE, server: str = "") -> None:
	"""Provision, time, and tear down ONE VM — validate the harness end-to-end
	before spending on the full interleaved run."""
	server_name = server or _active_scaleway_server()
	print(f"[bench] smoke server={server_name} image={image}")
	sample = _measure_one(server_name, image)
	print(f"[bench] smoke OK: {_fmt(sample)}")
	print(sample)


# ----- one VM: provision, time the markers, tear down -----------------------


def _measure_one(server_name: str, image: str) -> dict:
	"""Provision one VM, read its boot profile off the guest's own clocks (via
	the host's routed-tap path), then tear it down. Returns provision_ms plus the
	guest markers (kernel_ms/userspace_ms/total_ms/sshd_ms/crng_ms/hwrng/
	top_blame — see the module docstring)."""
	conn = connection_for_server(frappe.get_doc("Server", server_name))
	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": f"boot-bench {image}",
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

	try:
		task = _wait_for_provision_task(vm_name, timeout_seconds=180)
		provision_ms = (task["ended"] - task["started"]).total_seconds() * 1000.0
		vm.reload()
		if not vm.ipv6_address:
			raise AssertionError(f"VM {vm_name} has no ipv6_address after provision")
		guest = vm.ipv6_address

		with ssh_key_file(conn.ssh_private_key) as key:
			_stage_probe_key(conn, key)
			markers = _host_probe_boot(conn, key, guest)

		return {
			"vm": vm_name,
			"image": image,
			"provision_ms": provision_ms,
			**markers,
		}
	finally:
		_terminate(vm_name)


def _stage_probe_key(conn, key: str) -> None:
	"""Drop the ephemeral guest private key onto the host at /tmp/hp.key so the
	host-side loop can ssh into the guest. base64 so no quoting hazard."""
	import base64

	priv_b64 = base64.b64encode(ephemeral_private_key().encode()).decode()
	run_ssh(
		conn,
		key,
		f"echo {priv_b64} | base64 -d > /tmp/hp.key && chmod 600 /tmp/hp.key",
		timeout_seconds=20,
	)


def _host_probe_boot(conn, key: str, guest: str) -> dict:
	"""From the host: wait for the guest to answer SSH, then read its FULL boot
	profile off the guest's own clocks (systemd-analyze + dmesg). Because the
	guest usually finishes booting during the multi-second provision, a
	from-loop-start wall-clock would collapse to ~0 and tell us nothing — the
	guest's OWN monotonic accounting is what isolates the image's boot cost.

	Returns any of: kernel_ms, userspace_ms, total_ms, sshd_ms (ssh.service
	active), crng_ms (kernel `crng init done`), hwrng (rng_current string),
	top_blame (slowest unit)."""
	# 1. Block until the guest answers `ssh true` (host-side, routed-tap).
	out, _, _ = run_ssh(
		conn,
		key,
		_HOST_WAIT.format(guest=guest, hold=SSH_DEADLINE_SECONDS),
		timeout_seconds=SSH_DEADLINE_SECONDS + 30,
	)
	if "SSH_READY" not in out:
		print(f"[bench] WARNING: guest {guest} never answered SSH host-side")
		return {}

	# 2. One guest read for the whole boot profile, tagged so we can parse it.
	out, _, _ = run_ssh(conn, key, _GUEST_PROFILE.format(guest=guest), timeout_seconds=60)
	return _parse_profile(out)


# Host-side: poll the guest until `ssh true` returns 0 (or the hold elapses),
# printing SSH_READY on success. No wall-clock markers — the guest's own clock
# (read next) is authoritative for the image comparison.
_HOST_WAIT = r"""
guest={guest}
end=$(( $(date +%s) + {hold} ))
while [ $(date +%s) -lt $end ]; do
  if timeout 4 ssh -i /tmp/hp.key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o BatchMode=yes -o ConnectTimeout=3 root@"$guest" true 2>/dev/null; then
    echo SSH_READY; break
  fi
  sleep 0.5
done
"""

# Host-side: read the guest's boot profile in one shot, each value on a tagged
# line. `S <cmd>` runs the cmd on the guest over the staged key.
#
# Every marker here answers "when can the user do X?" from the guest's OWN
# monotonic clock, so the numbers are image-attributable with zero host offset:
#   * ANALYZE      — kernel/userspace/total to the default target.
#   * SSHD_US      — ssh.service active: the "when can I log in?" number, the one
#                    we are optimizing toward.
#   * NET_US       — network.target active (atlas-network done): when egress works.
#   * MULTI_US     — multi-user.target active: userspace fully up.
#   * CRNG / HWRNG — entropy markers (see module docstring).
#   * BLAME        — the single slowest unit (headline regression sentinel).
#   * BLAME5       — top-5 units, ';'-joined, so a shift in the serial gate shows.
#   * CHAIN        — the critical-chain leaf→root path, '|'-joined: the SERIAL
#                    path that actually gates boot. This is where a Tier-2 strip
#                    proves it moved the gate, not just some parallel unit.
#   * RUNNING      — count of running services + their names: daemon-count wins
#                    (2.1 networkd / 2.2 udevd) show here even at ~0 boot-ms.
#   * FAILED       — count of failed units: a strip that breaks a unit surfaces.
#   * PSS_KB       — total userspace PSS (KB): the RAM footprint at idle.
#   * PROCS        — userspace process count.
_GUEST_PROFILE = r"""
g={guest}
S() {{ ssh -i /tmp/hp.key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o BatchMode=yes -o ConnectTimeout=5 root@"$g" "$1" 2>/dev/null; }}
echo "ANALYZE=$(S 'systemd-analyze 2>/dev/null | head -1')"
echo "SSHD_US=$(S 'systemctl show ssh.service -p ActiveEnterTimestampMonotonic --value 2>/dev/null || systemctl show sshd.service -p ActiveEnterTimestampMonotonic --value 2>/dev/null')"
echo "SSHSOCK_US=$(S 'systemctl show ssh.socket -p ActiveEnterTimestampMonotonic --value 2>/dev/null')"
echo "NET_US=$(S 'systemctl show network.target -p ActiveEnterTimestampMonotonic --value 2>/dev/null')"
echo "MULTI_US=$(S 'systemctl show multi-user.target -p ActiveEnterTimestampMonotonic --value 2>/dev/null')"
echo "CRNG=$(S 'dmesg 2>/dev/null | grep -m1 "crng init done"')"
echo "HWRNG=$(S 'cat /sys/class/misc/hw_random/rng_current 2>/dev/null')"
echo "BLAME=$(S 'systemd-analyze blame 2>/dev/null | head -1')"
echo "BLAME5=$(S 'systemd-analyze blame 2>/dev/null | head -5 | tr "\n" ";"')"
echo "CHAIN=$(S 'systemd-analyze critical-chain --no-pager 2>/dev/null | sed 1,2d | sed "s/[^a-zA-Z0-9@._-]//g" | tr "\n" "|"')"
echo "RUNNING=$(S 'systemctl list-units --type=service --state=running --no-legend --no-pager 2>/dev/null | wc -l')"
echo "RUNNAMES=$(S 'systemctl list-units --type=service --state=running --no-legend --no-pager 2>/dev/null | awk "{{print \$1}}" | tr "\n" ";"')"
echo "FAILED=$(S 'systemctl --failed --no-legend --no-pager 2>/dev/null | wc -l')"
echo "MEM=$(S 'for d in /proc/[0-9]*; do r=$d/smaps_rollup; [ -r "$r" ] && awk "/^Pss:/{{p+=\$2}} END{{print p+0}}" $r; done | awk "{{s+=\$1}} END{{print s+0}}"')"
echo "PROCS=$(S 'ls -d /proc/[0-9]* 2>/dev/null | wc -l')"
"""


def _parse_profile(out: str) -> dict:
	"""Parse the tagged _GUEST_PROFILE output into numeric markers (ms) plus the
	categorical hwrng / top_blame strings."""
	markers: dict = {}
	for line in out.splitlines():
		line = line.strip()
		if line.startswith("ANALYZE="):
			text = line[len("ANALYZE=") :]
			k = _dur_ms(text, r"([\d.]+m?s) \(kernel\)")
			u = _dur_ms(text, r"([\d.]+m?s) \(userspace\)")
			t = _dur_ms(text, r"= ([\d.]+m?s)\s*$")
			if k is not None:
				markers["kernel_ms"] = k
			if u is not None:
				markers["userspace_ms"] = u
			if t is not None:
				markers["total_ms"] = t
		elif line.startswith("SSHD_US="):
			v = line[len("SSHD_US=") :]
			if v.isdigit() and int(v) > 0:
				markers["sshd_ms"] = int(v) / 1000.0
		elif line.startswith("SSHSOCK_US="):
			v = line[len("SSHSOCK_US=") :]
			if v.isdigit() and int(v) > 0:
				markers["sshsock_ms"] = int(v) / 1000.0
		elif line.startswith("NET_US="):
			v = line[len("NET_US=") :]
			if v.isdigit() and int(v) > 0:
				markers["network_ms"] = int(v) / 1000.0
		elif line.startswith("MULTI_US="):
			v = line[len("MULTI_US=") :]
			if v.isdigit() and int(v) > 0:
				markers["multiuser_ms"] = int(v) / 1000.0
		elif line.startswith("CRNG="):
			m = re.search(r"\[\s*([\d.]+)\]", line)
			if m:
				markers["crng_ms"] = float(m.group(1)) * 1000.0
		elif line.startswith("HWRNG="):
			markers["hwrng"] = line[len("HWRNG=") :].strip() or "none"
		elif line.startswith("BLAME="):
			markers["top_blame"] = line[len("BLAME=") :].strip()
		elif line.startswith("BLAME5="):
			markers["blame5"] = line[len("BLAME5=") :].strip().strip(";")
		elif line.startswith("CHAIN="):
			markers["critical_chain"] = line[len("CHAIN=") :].strip().strip("|")
		elif line.startswith("RUNNING="):
			v = line[len("RUNNING=") :].strip()
			if v.isdigit():
				markers["running_services"] = int(v)
		elif line.startswith("RUNNAMES="):
			markers["running_names"] = line[len("RUNNAMES=") :].strip().strip(";")
		elif line.startswith("FAILED="):
			v = line[len("FAILED=") :].strip()
			if v.isdigit():
				markers["failed_units"] = int(v)
		elif line.startswith("MEM="):
			v = line[len("MEM=") :].strip()
			if v.isdigit():
				markers["userspace_pss_kb"] = int(v)
		elif line.startswith("PROCS="):
			v = line[len("PROCS=") :].strip()
			if v.isdigit():
				markers["proc_count"] = int(v)
	return markers


def _dur_ms(text: str, pattern: str) -> float | None:
	"""Pull a systemd duration token (e.g. '621ms', '11.028s', '1min 2.3s') out
	of `text` via `pattern` (group 1 = the token) and return milliseconds."""
	m = re.search(pattern, text)
	if not m:
		return None
	token = m.group(1)
	total = 0.0
	minute = re.search(r"([\d.]+)min", token)
	if minute:
		total += float(minute.group(1)) * 60_000.0
	sub = re.search(r"([\d.]+)ms", token)
	if sub:
		return total + float(sub.group(1))
	sec = re.search(r"([\d.]+)s", token)
	if sec:
		return total + float(sec.group(1)) * 1000.0
	return total or None


def _wait_for_provision_task(vm_name: str, timeout_seconds: int) -> dict:
	"""Poll the provision-vm Task for this VM to Success; return its row.

	Raises on Failure or timeout. `started`/`ended` are datetimes."""
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		rows = frappe.get_all(
			"Task",
			filters={"virtual_machine": vm_name, "script": "provision-vm"},
			fields=["name", "status", "started", "ended"],
			order_by="creation desc",
			limit=1,
		)
		if rows:
			row = rows[0]
			if row["status"] == "Success" and row["ended"]:
				return row
			if row["status"] == "Failure":
				raise AssertionError(f"provision-vm Task {row['name']} for {vm_name} Failed")
		time.sleep(1)
	raise AssertionError(f"provision-vm Task for {vm_name} did not Succeed within {timeout_seconds}s")


def _terminate(vm_name: str) -> None:
	if frappe.db.exists("Virtual Machine", vm_name):
		frappe.db.rollback()  # drop any stale in-txn copy before reloading
		vm = frappe.get_doc("Virtual Machine", vm_name)  # fresh load: avoid TimestampMismatch
		if vm.status != "Terminated":
			try:
				vm.terminate()
				frappe.db.commit()
			except Exception:
				print(f"[bench] WARNING: terminate {vm_name} failed — drop by hand")
				traceback.print_exc()


# ----- reporting ------------------------------------------------------------


def _active_scaleway_server() -> str:
	rows = frappe.get_all(
		"Server",
		filters={"status": "Active", "provider_type": "Scaleway"},
		pluck="name",
	)
	if not rows:
		raise AssertionError("no Active Scaleway server — provision one first")
	return rows[0]


# The numeric (millisecond) markers we aggregate + median, in report order.
# network_ms / multiuser_ms / sshd_ms are the "when can I do X?" boot markers;
# sshd_ms is the headline target (time-to-SSH).
_NUMERIC_MARKERS = (
	"provision_ms",
	"kernel_ms",
	"userspace_ms",
	"total_ms",
	"network_ms",
	"sshsock_ms",
	"sshd_ms",
	"multiuser_ms",
	"crng_ms",
)

# Non-time markers aggregated as medians (footprint / daemon-count), reported
# separately from the millisecond timers.
_COUNT_MARKERS = (
	("userspace_pss_kb", "idle PSS", "MB", 1024.0),
	("running_services", "running svcs", "", 1.0),
	("failed_units", "failed units", "", 1.0),
	("proc_count", "procs", "", 1.0),
)


def _fmt(sample: dict) -> str:
	def ms(key):
		v = sample.get(key)
		return f"{v / 1000:.2f}s" if v is not None else "—"

	pss = sample.get("userspace_pss_kb")
	mem = f"{pss / 1024:.0f}MB" if pss is not None else "—"
	return (
		f"prov={ms('provision_ms')} kernel={ms('kernel_ms')} "
		f"userspace={ms('userspace_ms')} net={ms('network_ms')} "
		f"ssh.sock={ms('sshsock_ms')} boot->sshd={ms('sshd_ms')} multiuser={ms('multiuser_ms')} "
		f"crng={ms('crng_ms')} hwrng={sample.get('hwrng', '?')} "
		f"pss={mem} run={sample.get('running_services', '?')} "
		f"failed={sample.get('failed_units', '?')} procs={sample.get('proc_count', '?')}"
	)


def _report(old_image: str, new_image: str, results: dict[str, list[dict]]) -> None:
	print("\n" + "=" * 78)
	print("IMAGE BOOT BENCHMARK — boot markers read from the GUEST's own clocks")
	print("(systemd-analyze + dmesg + /proc), reached over the host's routed-tap path.")
	print("kernel/userspace/total: systemd-analyze. net: network.target active.")
	print("boot->sshd: ssh.service active (TIME-TO-SSH, the target). multiuser:")
	print("multi-user.target active. crng: kernel 'crng init done'. hwrng: bound RNG.")
	print("pss: idle userspace PSS. run: running services. procs: userspace proc count.")
	print("Per-VM lines also show blame top5, the critical-chain, and running svc names.")
	print("=" * 78)

	for image in (old_image, new_image):
		label = "OLD" if image == old_image else "NEW"
		samples = results[image]
		print(f"\n{label}  {image}  (n={len(samples)})")
		if not samples:
			print("  no successful runs")
			continue
		for s in samples:
			print(f"  {s['vm'][:8]}  {_fmt(s)}")
			if s.get("blame5"):
				print(f"            blame top5: {s['blame5']}")
			elif s.get("top_blame"):
				print(f"            slowest unit: {s['top_blame']}")
			if s.get("critical_chain"):
				print(f"            crit-chain: {s['critical_chain']}")
			if s.get("running_names"):
				print(f"            running:    {s['running_names']}")
		for key in _NUMERIC_MARKERS:
			values = [s[key] for s in samples if s.get(key) is not None]
			if values:
				med = statistics.median(values) / 1000
				mn = min(values) / 1000
				mx = max(values) / 1000
				print(f"  {key:13s} median={med:6.2f}s  min={mn:6.2f}s  max={mx:6.2f}s  (n={len(values)})")
		for key, label_txt, unit, div in _COUNT_MARKERS:
			values = [s[key] for s in samples if s.get(key) is not None]
			if values:
				med = statistics.median(values) / div
				mn = min(values) / div
				mx = max(values) / div
				print(
					f"  {label_txt:13s} median={med:6.1f}{unit}  min={mn:6.1f}  max={mx:6.1f}  (n={len(values)})"
				)

	# Head-to-head deltas on the boot markers that actually move.
	print("\n" + "-" * 78)
	print("DELTA (NEW - OLD) on medians; negative = NEW is faster/lighter")
	for key in (
		"kernel_ms",
		"userspace_ms",
		"total_ms",
		"network_ms",
		"sshsock_ms",
		"sshd_ms",
		"multiuser_ms",
		"crng_ms",
	):
		old_v = [s[key] for s in results[old_image] if s.get(key) is not None]
		new_v = [s[key] for s in results[new_image] if s.get(key) is not None]
		if old_v and new_v:
			delta = (statistics.median(new_v) - statistics.median(old_v)) / 1000
			print(f"  {key:13s} {delta:+.2f}s")
	for key, label_txt, unit, div in _COUNT_MARKERS:
		old_v = [s[key] for s in results[old_image] if s.get(key) is not None]
		new_v = [s[key] for s in results[new_image] if s.get(key) is not None]
		if old_v and new_v:
			delta = (statistics.median(new_v) - statistics.median(old_v)) / div
			print(f"  {label_txt:13s} {delta:+.1f}{unit}")
	print("=" * 78)
