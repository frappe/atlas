#!/usr/bin/env python3
# Adopt this VM's identity from the Firecracker metadata service — run INSIDE
# the guest, installed + enabled by warm.sh so it is ALIVE (mid-loop) in the
# RAM the warm bake freezes. Every clone restored from that golden wakes with
# the golden's identity in RAM and an UNTOUCHED disk (the host must never
# mutate a warm clone's disk offline — the frozen page cache has to keep
# matching it), so the identity arrives over the one channel that is per-clone
# and cache-safe: MMDS at 169.254.169.254 (provision-vm.py stages the payload;
# vm-restore.py PUTs it before resume, and the cold-boot fallback preloads it
# via the launcher's --metadata).
#
# The loop: poll MMDS once a second; when it serves an identity whose uuid
# differs from the one this disk last adopted (/etc/atlas-vm-uuid), apply it —
# fresh SSH host keys FIRST (so the controller's first successful connection
# already sees the new identity), then machine-id/hostname/authorized_keys and
# the on-disk network env (so a later plain reboot of the clone comes up
# correctly), and the live network LAST (bringing the clone's addresses up is
# what makes it reachable, i.e. the externally visible "freshen done" signal).
# /etc/atlas-vm-uuid is written at the very end as the applied marker.
#
# On the golden at bake time MMDS is empty, so the loop idles — exactly the
# frozen state a restore should wake into. Stdlib only; logs go to journald.

import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

MMDS_IP = "169.254.169.254"
MMDS_URL = f"http://{MMDS_IP}/identity"
VM_UUID_PATH = "/etc/atlas-vm-uuid"
NETWORK_ENV_PATH = "/etc/atlas-network.env"
POLL_SECONDS = 1.0

# linux/random.h RNDRESEEDCRNG (_IO('R', 0x07)): force the kernel CRNG to
# reseed from the input pool right now. Root-only.
RNDRESEEDCRNG = 0x5207


def network_env(identity: dict) -> str:
	"""The /etc/atlas-network.env content for this identity — the same shape the
	host writes at a cold provision, so atlas-network.service configures the
	clone correctly on any later plain reboot."""
	return (
		f"VIRTUAL_MACHINE_IPV6={identity['ipv6']}\n"
		f"VIRTUAL_MACHINE_IPV4={identity['ipv4_cidr']}\n"
		f"VIRTUAL_MACHINE_IPV4_GATEWAY={identity['ipv4_gateway']}\n"
	)


def hosts_lines(existing: str, hostname: str) -> str:
	"""/etc/hosts with the 127.0.1.1 convenience entry repointed at `hostname`
	(drop any previous 127.0.1.1 line — the golden's — and append ours)."""
	kept = [line for line in existing.splitlines() if not line.strip().startswith("127.0.1.1")]
	kept.append(f"127.0.1.1\t{hostname}")
	return "\n".join(kept) + "\n"


def _run(*argv: str, check: bool = False) -> None:
	result = subprocess.run(argv, capture_output=True, text=True)
	if result.returncode != 0:
		print(f"freshen: {' '.join(argv)} -> {result.returncode}: {result.stderr.strip()}", flush=True)
		if check:
			raise RuntimeError(f"{argv[0]} failed")


def _fetch_identity() -> dict | None:
	request = urllib.request.Request(MMDS_URL, headers={"Accept": "application/json"})
	try:
		with urllib.request.urlopen(request, timeout=2) as response:
			return json.loads(response.read().decode())
	except (urllib.error.URLError, OSError, ValueError):
		return None


def _adopted_uuid() -> str:
	try:
		# nosemgrep: frappe-security-file-traversal -- guest script; reads the fixed VM_UUID_PATH marker, not untrusted web input
		with open(VM_UUID_PATH) as handle:
			return handle.read().strip()
	except OSError:
		return ""


def _reseed_rng(uuid: str) -> None:
	"""Mix per-clone data into the kernel entropy pool and force a CRNG reseed.
	Writing to /dev/urandom mixes without crediting entropy; RNDRESEEDCRNG then
	makes the CRNG consume it immediately, so the very next getrandom() output
	already diverges per clone."""
	# nosemgrep: frappe-security-file-traversal -- guest script; writes the fixed /dev/urandom device, not untrusted web input
	with open("/dev/urandom", "wb") as handle:
		handle.write(f"{uuid}:{time.time_ns()}".encode() + os.urandom(32))
		fcntl.ioctl(handle, RNDRESEEDCRNG)


def _apply(identity: dict) -> None:
	hostname = identity["hostname"]

	# 0. Diverge the kernel CSPRNG before ANY key material is generated. N
	# clones resume from byte-identical RAM — identical entropy pools.
	# Firecracker's VMGenID device does reseed the CRNG per restore, but with a
	# documented race window (random-for-clones.md), and this loop runs within
	# a second of resume — two clones ssh-keygen'ing IDENTICAL host keys was
	# observed on a real host. Mixing per-clone data (the uuid + the resumed
	# clock) and forcing the reseed makes divergence deterministic, not a race.
	_reseed_rng(identity["uuid"])

	# 1. SSH identity first: fresh host keys + a listener restart, while the
	# clone is still unreachable — so the controller's first successful
	# connection (which pins the key via accept-new) already sees this clone's
	# key, never the golden's. Ubuntu's ssh may be socket-activated (per-
	# connection sshd reads keys freshly); restart both forms, tolerantly.
	_run("sh", "-c", "rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub")
	_run(
		"ssh-keygen",
		"-q",
		"-t",
		"ed25519",
		"-f",
		"/etc/ssh/ssh_host_ed25519_key",
		"-N",
		"",
		"-C",
		f"root@{hostname}",
		check=True,
	)
	_run("systemctl", "try-restart", "ssh.service")
	_run("systemctl", "try-restart", "ssh.socket")
	# nosemgrep: frappe-security-file-traversal -- guest script; writes the fixed /root/.ssh/authorized_keys path, not untrusted web input
	with open("/root/.ssh/authorized_keys", "w") as handle:
		handle.write(identity["ssh_public_key"] + "\n")

	# 2. OS identity on disk (machine-id is also what the e2e distinctness gate
	# reads). The RUNNING systemd/dbus keep the golden's machine-id in RAM until
	# the clone's next real reboot — accepted; everything that reads the files
	# sees the clone's.
	for machine_id_path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
		_run("sh", "-c", f"echo {identity['machine_id']} > {machine_id_path}")
	# nosemgrep: frappe-security-file-traversal -- guest script; writes the fixed /etc/hostname path, not untrusted web input
	with open("/etc/hostname", "w") as handle:
		handle.write(hostname + "\n")
	_run("hostname", hostname)
	# nosemgrep: frappe-security-file-traversal -- guest script; reads the fixed /etc/hosts path, not untrusted web input
	with open("/etc/hosts") as handle:
		existing = handle.read()
	# nosemgrep: frappe-security-file-traversal -- guest script; writes the fixed /etc/hosts path, not untrusted web input
	with open("/etc/hosts", "w") as handle:
		handle.write(hosts_lines(existing, hostname))

	# 3. The on-disk network env, so a later plain reboot of this clone brings
	# its own addresses up through the ordinary atlas-network.service path.
	# nosemgrep: frappe-security-file-traversal -- guest script; writes the fixed NETWORK_ENV_PATH, not untrusted web input
	with open(NETWORK_ENV_PATH, "w") as handle:
		handle.write(network_env(identity))

	# 4. Clone-entropy hygiene: the seed was deleted at bake; keep it gone.
	_run("rm", "-f", "/var/lib/systemd/random-seed")

	# 5. Live network LAST: drop the golden's addresses, bring up this clone's.
	# Becoming reachable on the clone's /128 is the externally visible "freshen
	# done" — everything above must already hold by then. Global scope only, so
	# the link-local (the v6 default route's nexthop interface) survives.
	_run("ip", "-6", "addr", "flush", "dev", "eth0", "scope", "global")
	_run("ip", "-4", "addr", "flush", "dev", "eth0", "scope", "global")
	_run("ip", "-6", "addr", "replace", f"{identity['ipv6']}/128", "dev", "eth0")
	_run("ip", "-6", "route", "replace", "default", "via", "fe80::1", "dev", "eth0")
	_run("ip", "-4", "addr", "replace", identity["ipv4_cidr"], "dev", "eth0")
	_run("ip", "-4", "route", "replace", "default", "via", identity["ipv4_gateway"], "dev", "eth0")
	_run("ip", "route", "replace", MMDS_IP, "dev", "eth0")
	_run("sh", "-c", 'rm -f /etc/resolv.conf; echo "nameserver 2606:4700:4700::1111" > /etc/resolv.conf')

	# 6. The clock resumed at the snapshot instant (always in the past — a
	# forward jump); kick time sync now that the network is the clone's.
	_run("systemctl", "try-restart", "systemd-timesyncd.service")

	# 7. The applied marker, last — also what tells a plain reboot of this clone
	# (and an idempotent re-poll) that there is nothing left to adopt.
	# nosemgrep: frappe-security-file-traversal -- guest script; writes the fixed VM_UUID_PATH marker, not untrusted web input
	with open(VM_UUID_PATH, "w") as handle:
		handle.write(identity["uuid"] + "\n")
	print(f"freshen: adopted identity {identity['uuid']} ({hostname})", flush=True)


def main() -> None:
	while True:
		# The MMDS route must exist before the first GET and is dropped by the
		# address flush in _apply; replace is idempotent, so just re-assert it.
		_run("ip", "route", "replace", MMDS_IP, "dev", "eth0")
		payload = _fetch_identity()
		if payload:
			identity = payload if "uuid" in payload else payload.get("identity", {})
			uuid = identity.get("uuid", "")
			if uuid and uuid != _adopted_uuid():
				try:
					_apply(identity)
				except Exception as error:  # keep polling; a retry next tick may heal
					print(f"freshen: apply failed: {error}", file=sys.stderr, flush=True)
		time.sleep(POLL_SECONDS)


if __name__ == "__main__":
	main()
