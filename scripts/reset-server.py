#!/usr/bin/env python3
# Reset a bootstrapped host back to its just-bootstrapped state. DESTRUCTIVE.
#
# This is the whole-host analogue of terminate-vm.py: where terminate tears down
# one VM's on-host state, reset-server sweeps EVERY VM, image, snapshot, forward
# tunnel, and stray networking artifact off the host — leaving exactly what
# bootstrap-server.py produced (the atlas VG + empty pool0, the venv under
# /var/lib/atlas/{venv,bin}, the host hardening, atlas-pool.service). After this
# the host is immediately provision-ready; it does NOT need re-bootstrapping.
#
# It exists because a host's on-disk state can drift out of sync with the Frappe
# DB (e.g. the DB's VM/Image/Snapshot rows were lost while the host kept its
# LVs/units/jails). Rather than reconcile row-by-row, reset-server wipes the host
# clean so the controller can start from an empty, known slate.
#
# Idempotent and best-effort: every step tolerates already-gone state, so a
# partial run can be re-run. There is no machine-readable result — a wipe just
# wipes — so it prints human progress lines, not an ATLAS_RESULT= line.
#
# What it KEEPS (the bootstrap floor):
#   - the atlas VG and the empty thin pool `pool0`
#   - /var/lib/atlas/{venv,bin,run} and the `atlas` console script
#   - host hardening (CIS sysctls, sshd drop-in, module blocklist), the mgmt
#     firewall, atlas-pool.service, and the base nft `inet atlas` table scaffold
#
# What it REMOVES:
#   - every firecracker-vm@<uuid> unit + /var/lib/atlas/virtual-machines/*
#   - every atlas-mig6-* forward-tunnel unit (migration carriers)
#   - every atlas-vm-/atlas-data-/atlas-snap-/atlas-datasnap-/atlas-clonemeta-
#     LV, AND every atlas-image-* base-image LV (full scratch: no images kept)
#   - /var/lib/atlas/images/* and /var/lib/atlas/snapshots/*
#   - every atlas network namespace + host-side veth/tap/mig6 link
#   - any bound /dev/nbd* client, and any leftover dm-clone target
#   - every per-VM rule in the nft `inet atlas forward` chain, the firewall
#     chain, and every NDP proxy entry the VMs installed

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs
from atlas.lvm import ThinPool
from atlas.paths import (
	ATLAS_PYTHON,
	IMAGES_DIRECTORY,
	SNAPSHOTS_DIRECTORY,
	VIRTUAL_MACHINES_DIRECTORY,
)

# The atlas-managed dm-clone target and nbd client leftovers a mid-flight
# migration can strand on the target host (mirrors migration-cutover-target.py).
_MIGRATION_DM_SUFFIXES = ("", "-data")


@dataclass(frozen=True)
class ResetInputs(TaskInputs):
	"""Reset a bootstrapped host back to its just-bootstrapped state. DESTRUCTIVE."""

	command: typing.ClassVar[str] = "reset-server"


def main() -> None:
	ResetInputs.from_args()
	pool = ThinPool()

	stop_virtual_machines()
	stop_forward_tunnels()
	teardown_networking()
	disconnect_nbd_and_dm_clone()
	remove_logical_volumes(pool)
	clear_state_directories()

	print("Reset complete — host is back to its just-bootstrapped state.")


def stop_virtual_machines() -> None:
	"""Disable+stop every per-VM firecracker unit and remove its directory. Runs
	the durable vm-network-down hook per VM first (same as terminate-vm's fallback
	teardown) so the netns/veth/nft/NDP state a stopped-but-not-terminated VM left
	behind is cleaned even when the unit's ExecStopPost never ran."""
	for uuid in _list_vm_directories():
		unit = f"firecracker-vm@{uuid}.service"
		# Tolerate failure: the unit may already be gone or never have started.
		run("sudo systemctl disable --now {}", unit, check=False)
		# vm-network-down.py is the durable, idempotent teardown hook (positional
		# uuid, imports the package under /var/lib/atlas/bin). Invoke it under the
		# venv python — the same interpreter the unit's ExecStopPost uses.
		run(
			"sudo {} /var/lib/atlas/bin/vm-network-down.py {}",
			ATLAS_PYTHON,
			uuid,
			check=False,
		)
		# The directory carries the jail tree (kernel, config, socket, rootfs
		# NODE). The LV the node points at is a separate object removed later.
		run("sudo rm -rf {}", f"{VIRTUAL_MACHINES_DIRECTORY}/{uuid}", check=False)
	# Reset any units left in a failed state so the names are reusable and
	# `systemctl status` on a fresh VM is clean.
	run("sudo systemctl reset-failed 'firecracker-vm@*'", check=False)


def stop_forward_tunnels() -> None:
	"""Stop every atlas-mig6-<port> transient forward-tunnel unit (the socat
	carriers migration-forward-up.py starts). Stopping the unit tears down the
	socat process; the tun device it owned disappears with it."""
	for unit in _list_units("atlas-mig6-*"):
		run("sudo systemctl stop {}", unit, check=False)
	run("sudo systemctl reset-failed 'atlas-mig6-*'", check=False)


def teardown_networking() -> None:
	"""Sweep any VM networking artifact the per-VM teardown above did not reach:
	stray atlas netns, host-side veth/tap/mig6 links, and every NDP proxy entry.
	Best-effort and idempotent — a missing device/namespace is not an error.

	The per-VM vm-network-down already handled the VMs it knew about; this catches
	orphans (a link whose VM directory was already gone). The masquerade rule and
	the base `inet atlas forward` chain scaffold are host-wide bootstrap state and
	are intentionally left in place — only per-VM rules are removed, by the
	per-VM hook above; the base table stays exactly as bootstrap built it."""
	# atlas network namespaces (bootstrap creates none; every one here is a VM's).
	for netns in _list_netns():
		if netns.startswith("atlas-"):
			run("sudo ip netns del {}", netns, check=False)

	# Host-side veth/tap/mig6 links whose peer/namespace is already gone.
	for link in _list_atlas_links():
		run("sudo ip link del {}", link, check=False)

	# Every NDP proxy entry (proxy-NDP is only ever installed per VM).
	for entry in _list_ndp_proxy():
		address, device = entry
		run("sudo ip -6 neigh del proxy {} dev {}", address, device, check=False)


def disconnect_nbd_and_dm_clone() -> None:
	"""Disconnect any bound nbd client and remove any lingering dm-clone target —
	the two device-mapper/nbd leftovers a mid-flight migration can strand on a
	host. Idempotent: a free device / absent target is a no-op."""
	for device in _list_bound_nbd():
		run("sudo nbd-client -d {}", device, check=False)

	# dm-clone targets are named by the collapse key (uuid / uuid-data). We can't
	# enumerate VM uuids from the DB here, so sweep every dm target whose name
	# looks like an atlas clone and remove it. `dmsetup ls` lists live targets.
	for name in _list_dm_targets():
		run("sudo dmsetup remove {}", name, check=False)


def remove_logical_volumes(pool: ThinPool) -> None:
	"""Remove every atlas-managed LV EXCEPT the thin pool itself. This includes
	the base-image LVs (atlas-image-*), which LogicalVolume.remove() deliberately
	refuses as protected shared state — a full scratch reset wants them gone, so
	we lvremove the whole set here by name, guarding only `pool0`. `pool0` and the
	VG survive so the host stays provision-ready without a re-bootstrap."""
	for name in _list_atlas_lvs(pool):
		if name == pool.pool_name:
			continue
		run("sudo lvremove -f {}", f"{pool.volume_group}/{name}", check=False)


def clear_state_directories() -> None:
	"""Empty the per-VM, image, and snapshot state directories (but keep the
	directories themselves, as bootstrap leaves them). venv/ and bin/ under
	/var/lib/atlas are the bootstrap floor and are untouched."""
	for directory in (VIRTUAL_MACHINES_DIRECTORY, IMAGES_DIRECTORY, SNAPSHOTS_DIRECTORY):
		# Remove the CONTENTS, not the directory: bootstrap created the dir 0755
		# root, and later provisions expect it to exist.
		run("sudo find {} -mindepth 1 -maxdepth 1 -exec rm -rf {} +", directory, "{}", check=False)


# --- host enumeration: read-only pokes, each tolerating absence ---------------


def _list_vm_directories() -> list[str]:
	out = run("ls -1 {}", VIRTUAL_MACHINES_DIRECTORY, check=False)
	return [line.strip() for line in out.splitlines() if line.strip()]


def _list_units(pattern: str) -> list[str]:
	# --plain drops the tree glyphs; --no-legend drops the header/footer, so each
	# row is `<unit> <load> <active> <sub> <description>` and token 0 is the unit.
	out = run("systemctl list-units {} --all --no-legend --plain", pattern, check=False)
	units = []
	for line in out.splitlines():
		line = line.strip()
		if not line:
			continue
		token = line.split()[0]
		if token.endswith(".service"):
			units.append(token)
	return units


def _list_netns() -> list[str]:
	out = run("ip netns list", check=False)
	# Each line is "<name>" or "<name> (id: N)"; take the first token.
	return [line.split()[0] for line in out.splitlines() if line.strip()]


def _list_atlas_links() -> list[str]:
	# Host-side halves are veth-*/tap*/mig6-*; a link line is "<idx>: <name>@peer:".
	out = run("ip -o link show", check=False)
	links = []
	for line in out.splitlines():
		parts = line.split(": ", 2)
		if len(parts) < 2:
			continue
		name = parts[1].split("@", 1)[0].strip()
		if name.startswith(("veth-", "tap", "mig6-")):
			links.append(name)
	return links


def _list_ndp_proxy() -> list[tuple[str, str]]:
	# `ip -6 neigh show proxy` lines read "<addr> dev <dev> proxy".
	out = run("ip -6 neigh show proxy", check=False)
	entries = []
	for line in out.splitlines():
		tokens = line.split()
		if "dev" in tokens:
			address = tokens[0]
			device = tokens[tokens.index("dev") + 1]
			entries.append((address, device))
	return entries


def _list_bound_nbd() -> list[str]:
	# A bound /dev/nbdN reports a non-zero size in /sys/block/nbdN/size.
	devices = []
	out = run("ls -1 /sys/block", check=False)
	for name in out.splitlines():
		name = name.strip()
		if not name.startswith("nbd"):
			continue
		size = run("cat {}", f"/sys/block/{name}/size", check=False).strip()
		if size and size != "0":
			devices.append(f"/dev/{name}")
	return devices


def _list_dm_targets() -> list[str]:
	# dm-clone targets a migration strands are named by the clone key; sweep any
	# live dm target (dmsetup ls) — atlas installs none at bootstrap, so on a
	# just-bootstrapped host this is empty and this is a no-op.
	out = run("sudo dmsetup ls --target clone", check=False)
	targets = []
	for line in out.splitlines():
		line = line.strip()
		if not line or line == "No devices found":
			continue
		targets.append(line.split()[0])
	return targets


def _list_atlas_lvs(pool: ThinPool) -> list[str]:
	out = run("sudo lvs --noheadings -o lv_name {}", pool.volume_group, check=False)
	return [line.strip() for line in out.splitlines() if line.strip()]


if __name__ == "__main__":
	main()
