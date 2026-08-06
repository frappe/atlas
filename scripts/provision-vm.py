#!/usr/bin/env python3
# Provision one Firecracker VM on this server. Single task: prepares disk,
# config, networking, then starts the systemd unit. Run once per VM.
#
# Successor to provision-vm.sh, the biggest task. Same typed Task contract as
# snapshot-vm.py / rebuild-vm.py: ProvisionInputs.from_args() parses the CLI
# flags that used to be env vars; there is no machine-readable result to emit
# (the controller only needs the exit code), so this prints a human
# "Provisioned ..." line like the original.
#
# Inputs (formerly environment variables, now --kebab-case CLI flags):
#   virtual_machine_name  - UUID, used for directory, tap, systemd instance
#   image_name            - directory under /var/lib/atlas/images
#   kernel_filename       - filename inside the image directory
#   rootfs_filename       - filename inside the image directory
#   vcpus                 - integer
#   memory_mb             - integer
#   disk_gb               - integer, final rootfs size for this VM
#   mac_address           - e.g. 06:00:01:02:03:04
#   tap_device            - e.g. atlas-<first 9 hex of vm name>
#   virtual_machine_ipv6  - the VM's address inside the server's /124
#   ipv4_host_cidr        - host side of the per-VM NAT44 /30, e.g. 100.64.0.9/30
#   ipv4_guest_cidr       - guest side of the same /30, e.g. 100.64.0.10/30
#   ipv4_gateway          - host side address (no mask), the guest's v4 gateway
#   ssh_public_key        - injected into the rootfs
#   atlas_fc_uid          - per-VM uid the jailer drops Firecracker to (gid == uid)
#   atlas_netns           - per-VM network namespace name
#   host_veth             - host-side veth interface name
#   namespace_veth        - namespace-side veth interface name
#   cgroup_arg (repeatable)   - one cgroup VALUE per flag, e.g.
#                               --cgroup-arg memory.max=… --cgroup-arg "cpu.max=Q P".
#                               The launcher prefixes each with --cgroup (see NOTE).
#   resource_arg (repeatable) - one resource-limit VALUE per flag; the launcher
#                               prefixes each with --resource-limit. Optional —
#                               omit on a hand run to default to no-file=1024.
#   snapshot_rootfs_path  - optional; a snapshot's /dev/atlas/<name> device path
#                           (clone path); empty for a base-image provision

import glob
import json
import os
import shlex
import sys
import typing
from dataclasses import dataclass, field

# Jailer `--resource-limit` fallback when --resource-arg is omitted (break-glass
# hand run). The controller passes the VM's full resource triple via
# atlas.networking.resource_limit_args(), but that is effectively the constant
# `no-file=1024` today, so an operator needn't type it. Kept here as a VALUE (the
# launcher prefixes --resource-limit); 1024 mirrors networking.MAX_OPEN_FILES.
DEFAULT_RESOURCE_ARGS = ["no-file=1024"]

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import install_directory, install_file, run, run_ok
from atlas._task import TaskInputs
from atlas.lvm import ThinPool, expose_device_in_jail
from atlas.paths import VirtualMachinePaths, image_directory
from atlas.rootfs import Identity, inject_identity, prepare_data_lv, prepare_lv


@dataclass(frozen=True)
class ProvisionInputs(TaskInputs):
	"""Provision one Firecracker VM on this server: disk, config, networking,
	then start the systemd unit."""

	command: typing.ClassVar[str] = "provision-vm"
	virtual_machine_name: str  # UUID; directory, tap, systemd instance, identity
	image_name: str  # directory under /var/lib/atlas/images
	kernel_filename: str  # filename inside the image directory
	rootfs_filename: str  # filename inside the image directory
	vcpus: int
	memory_mb: int
	disk_gb: int  # final rootfs size for this VM
	ssh_public_key: str  # injected into the rootfs
	# --- DERIVED flags. Every field below is a controller-allocated value, NOT
	# defaultable: a wrong value mis-wires the jail/network. They stay REQUIRED; the
	# break-glass ergonomic is the `help=` recipe naming the atlas.networking function
	# that computes each, so an operator can reproduce them by hand. (See spec/06.)
	mac_address: str = field(metadata={"help": "derived: atlas.networking.derive_mac(uuid)"})
	tap_device: str = field(metadata={"help": "derived: atlas.networking.derive_tap(uuid)"})
	virtual_machine_ipv6: str = field(
		metadata={"help": "controller-allocated: atlas.networking.allocate_ipv6(server) (DB row-lock scan)"}
	)
	ipv4_host_cidr: str = field(
		metadata={"help": "derived: atlas.networking.derive_ipv4_link(ipv6)[0] (host side of the NAT44 /30)"}
	)
	ipv4_guest_cidr: str = field(
		metadata={"help": "derived: atlas.networking.derive_ipv4_link(ipv6)[1] (guest side of the /30)"}
	)
	ipv4_gateway: str = field(
		metadata={"help": "derived: host side of ipv4_host_cidr without the mask (the guest's v4 gateway)"}
	)
	atlas_fc_uid: int = field(metadata={"help": "derived: atlas.networking.derive_uid(uuid) (gid == uid)"})
	atlas_netns: str = field(metadata={"help": "derived: atlas.networking.derive_netns(uuid)"})
	host_veth: str = field(
		metadata={"help": "derived: atlas.networking.derive_veth_pair(uuid)[0] (host-side veth)"}
	)
	namespace_veth: str = field(
		metadata={"help": "derived: atlas.networking.derive_veth_pair(uuid)[1] (namespace-side veth)"}
	)
	# Jailer cgroup/resource limits as REPEATABLE VALUE flags: --cgroup-arg
	# memory.max=… --cgroup-arg "cpu.max=100000 100000". Each flag carries the
	# VALUE only; _jailer_launch prefixes each with --cgroup / --resource-limit.
	#
	# Values-only (not the interleaved "--cgroup <v> --cgroup <v>" the shell's
	# ATLAS_CGROUP_ARGS held) for two reasons: a literal "--cgroup" token in an
	# append-list collides with argparse's flag parsing, and the prefix is a
	# constant the launcher owns anyway. This still kills the shell's mapfile
	# hack — a value with an internal space (cpu.max's "<quota> <period>") is one
	# argv token, no word-splitting. TaskInputs renders a list field as append.
	#
	# cgroup_arg stays REQUIRED: it encodes the VM's real memory/cpu limits; an empty
	# cgroup set would silently un-bound the VM, so failing loud is correct.
	cgroup_arg: list = field(
		metadata={
			"help": "REQUIRED cgroup VALUES (launcher emits `--cgroup <value>`); "
			"derived: atlas.networking.cgroup_args(memory_mb, cpu_max_cores, cpu_mode)"
		}
	)
	# resource_arg DEFAULTS to [] → DEFAULT_RESOURCE_ARGS at use, so a hand run can
	# omit it (it is effectively the constant no-file=1024). The controller still
	# passes the full set from atlas.networking.resource_limit_args(); the default is
	# additive and never alters that path.
	resource_arg: list = field(
		default_factory=list,
		metadata={
			"help": "resource-limit VALUES (launcher emits `--resource-limit <value>`); "
			f"omit to default to {DEFAULT_RESOURCE_ARGS}"
		},
	)
	# One optional source override: a snapshot rootfs path (clone path). Empty
	# means provision from the base image. Mirrors the shell's "${VAR:-}".
	snapshot_rootfs_path: str = ""  # a snapshot's /dev/atlas/<name> device path
	# Optional: the Atlas controller base URL the in-guest routing client POSTs to
	# (spec/18 self-service subdomain routing). Written to /etc/atlas-routing.env on
	# the cold path (via Identity) and carried in the warm clone's MMDS payload.
	# Empty for a VM whose controller did not inject it — the guest client then
	# no-ops, so an ordinary VM is unaffected. NON-SECRET (no token rides it).
	routing_base_url: str = ""
	# Optional: a Reserved IP attached to this VM (the VM's denormalized
	# public_ipv4). Empty for every ordinary VM. Carried so a rebuild of a VM that
	# already has an attached v4 re-creates its 1:1-NAT on first boot (the same
	# reason _ipv4_link_variables is re-injected on rebuild). Live attach/detach
	# of a *running* VM goes through vm-reserved-ip.py, not provision.
	reserved_ipv4: str = ""  # the attached Reserved IP, 1:1-NAT'd to the guest /30
	# Optional: the VM's private-plane identity on the WireGuard host mesh (§5).
	# private_address is the derived fdaa:: /128; tenant_prefix is its tenant /48.
	# Both empty for a VM with no tenant (operator-created) — vm-network-up reads them
	# with .get() and skips the whole private-plane block when absent, so an ordinary
	# VM's env is unchanged. Carried on provision + rebuild so a rebuild re-creates the
	# private routes + isolation rules on first boot, like reserved_ipv4.
	private_address: str = ""  # derived: atlas.networking.derive_private_address(tenant, uuid)
	tenant_prefix: str = ""  # derived: atlas.networking.derive_tenant_prefix(tenant)
	# Optional second writable data disk (the guest's /dev/vdb). 0 = none.
	# data_disk_format is an int (0/1), not a bool: the Task runner renders a bool
	# as a truthy string, so "0" would read True — int parses cleanly to 0/1.
	# data_disk_mount_at is the in-guest mount point (empty = don't format/mount).
	# data_snapshot_rootfs_path seeds the data disk from a data-disk snapshot LV
	# (clone); empty means a fresh blank data disk.
	data_disk_gb: int = 0
	data_disk_format: int = 1
	data_disk_mount_at: str = ""
	data_snapshot_rootfs_path: str = ""
	# Migration cutover (spec/24): the disk LV already exists (hydrated from the
	# source over NBD) and already carries the VM's SSH host identity, which must
	# survive the move (clients' known_hosts). With this set, the identity inject
	# PRESERVES the existing host keys instead of regenerating them — the same
	# rebuild/restore contract. 0 (birth: fresh keys) for every ordinary provision.
	preserve_host_keys: int = 0
	# Boot-then-hydrate migration (spec/24 §0): boot the guest on the dm-clone
	# read-through device (/dev/mapper/atlas-vm-<uuid>-clone) BEFORE hydration, so
	# the guest serves while blocks copy in the background. When set:
	#   - the disk LV is NOT (re)created — the clone's dest LV already exists and is
	#     being hydrated; snapshot_into/prepare_lv would race the live read-through.
	#   - identity is NOT injected here — it was already injected THROUGH the clone in
	#     the InjectingIdentity phase (mounting the plain LV would fault; the clone is
	#     the only mountable view, spec/24 §0.4).
	#   - the JAIL ROOTFS NODE is mknod'd at the CLONE device, not the plain LV, so
	#     Firecracker reads through the clone. At CollapseClone the clone table is
	#     reloaded to a linear map onto the same dest LV, keeping this fd valid.
	# Empty for every ordinary provision and for a change-address / cold cutover
	# (which collapse first, then provision against the plain LV as before).
	clone_rootfs_device: str = ""  # /dev/mapper/atlas-vm-<uuid>-clone
	# Optional warm-restore source: the durable directory holding a warm golden
	# snapshot's vmstate.bin/mem.bin/host-signature.json (paired with
	# snapshot_rootfs_path, which must be that golden's disk snapshot). When set,
	# the clone's disk is a bare CoW of the golden (no grow/UUID-reroll/identity
	# injection — the frozen RAM's filesystem cache must keep matching the disk),
	# the golden pair is hard-linked into the jail behind a READY marker, and the
	# clone's identity is staged as MMDS metadata for the in-guest freshen unit.
	warm_snapshot_directory: str = ""


def main() -> None:
	inputs = ProvisionInputs.from_args()
	pool = ThinPool()
	paths = VirtualMachinePaths(inputs.virtual_machine_name)
	image = image_directory(inputs.image_name)
	uid = inputs.atlas_fc_uid

	# 0. Verify image present. Fail loud with an actionable message so the
	#    operator knows to click Sync to Server before retrying. (Image sync is
	#    multi-minute and is intentionally not auto-triggered from provision.)
	#    The kernel is needed regardless of the rootfs source, so this probe
	#    stays even when the rootfs comes from a snapshot (clone path,
	#    snapshot_rootfs_path set).
	rootfs_image = f"{image}/{inputs.rootfs_filename}"
	if not os.path.isfile(rootfs_image):
		sys.exit(
			f"image '{inputs.image_name}' not present on server (missing "
			f"{rootfs_image}); run Sync to Server first"
		)

	# 0b. Per-VM uid collision guard. The uid is derived from the UUID and is
	#     almost always unique, but a mod collision is possible. If a *different*
	#     live VM's jail rootfs is already owned by this uid, fail loud rather
	#     than silently letting two VMs share a uid (which would break inter-jail
	#     isolation).
	_guard_uid_collision(uid, paths.rootfs_node)

	install_directory(paths.directory, mode="0700")
	install_directory(paths.log_directory, mode="0700")
	install_directory(paths.jail_root, mode="0700")
	install_directory(paths.api_socket_directory, mode="0700")

	warm = bool(inputs.warm_snapshot_directory)
	if warm and inputs.data_disk_gb > 0:
		sys.exit("a warm clone cannot carry a data disk; the golden was captured without one")

	# A (re)provision lays a fresh disk; a leftover memory snapshot would pair
	# stale RAM with it. Drop it so the next start cold-boots. For a warm clone,
	# a still-present marker proves the staged pair was never consumed (the
	# guest never ran), so re-staging below is safe on an idempotent re-run.
	marker_was_pending = run_ok("sudo test -f {}", paths.memory_snapshot_marker)
	run("sudo rm -rf {}", paths.memory_snapshot_directory)

	# 1. Per-VM disk LV. An instant CoW thin snapshot of an origin LV — the
	#    pristine image's base LV normally, or a snapshot LV when cloning
	#    (snapshot_rootfs_path is that snapshot's /dev/atlas/<name> device path).
	#    No full copy: unwritten blocks are shared with the origin. The per-VM
	#    identity injected in step 2 is freshly derived from THIS VM's UUID, so a
	#    clone never shares host keys or machine-id with its source. Same origin
	#    resolution and guards as rebuild.
	#
	#    Warm clone: the disk must stay a byte-exact CoW of the golden — the
	#    frozen RAM's filesystem cache references exactly those blocks, so ANY
	#    offline mutation (grow, tune2fs UUID reroll, identity injection) would
	#    corrupt the resumed guest. Bare snapshot_into only; the warm pair is
	#    staged only when this run created the disk (or the previous staging was
	#    never consumed) — RAM must never be restored over a disk that diverged.
	# Boot-on-clone only if the clone device is BOTH requested AND actually present on
	# the host. A collapse-forward retry (or any re-provision) may pass the clone path
	# after the disk already converged to the plain LV (clone removed at stop); fall
	# back to an ordinary plain-LV provision then, rather than fail on a missing clone.
	boot_on_clone = bool(inputs.clone_rootfs_device) and run_ok(
		"sudo dmsetup info {}", os.path.basename(inputs.clone_rootfs_device)
	)
	disk = pool.vm_disk(inputs.virtual_machine_name)
	if boot_on_clone:
		# Boot-then-hydrate migration (spec/24 §0): the disk is the dm-clone's dest
		# LV, already created by clone-target and hydrating live. Do NOT snapshot/grow
		# it — that would race the read-through and corrupt the copy. The clone device
		# is exposed as the jail rootfs node in step 4b.
		stage_warm = False
		if not disk.exists:
			sys.exit(
				f"boot-on-clone requested but dest LV {disk.name} does not exist; "
				"run migration-clone-target first (spec/24 §0)"
			)
	else:
		origin = _resolve_origin(inputs, pool)
		if warm:
			stage_warm = (not disk.exists) or marker_was_pending
			origin.snapshot_into(disk)
		else:
			stage_warm = False
			prepare_lv(origin, disk, inputs.disk_gb)

	# 1b. Optional data disk (the guest's /dev/vdb), the root disk's peer. A blank
	#     thin volume normally, or a CoW snapshot of a data-disk snapshot LV when
	#     cloning (data_snapshot_rootfs_path set). Built here, before identity
	#     injection, so its `atlas-data` ext4 label exists when the fstab
	#     LABEL=atlas-data line is written into the root rootfs in step 2.
	data_disk = None
	if inputs.data_disk_gb > 0:
		data_disk = pool.data_disk(inputs.virtual_machine_name)
		if boot_on_clone:
			# The data disk is its own dm-clone (atlas-vm-<uuid>-data-clone) hydrating
			# live, exactly like root. Its dest LV already exists; do NOT prepare it
			# (that would race the read-through). Exposed as the data clone node in 4c.
			if not data_disk.exists:
				sys.exit(
					f"boot-on-clone requested but data dest LV {data_disk.name} does not exist; "
					"run migration-clone-target first (spec/24 §0)"
				)
		else:
			data_origin = (
				pool.from_device(inputs.data_snapshot_rootfs_path)
				if inputs.data_snapshot_rootfs_path
				else None
			)
			prepare_data_lv(
				pool, data_disk, inputs.data_disk_gb, bool(inputs.data_disk_format), origin=data_origin
			)

	# 2. Inject this VM's identity (SSH key, network env, hostname, host
	#    keys, machine-id, data-disk fstab) into the disk. Mounts the LV device
	#    directly (no loop). The v4 egress link goes into the guest's network env
	#    here too, so clone/rebuild get it for free. Done outside the jail, before
	#    the jailer starts.
	#
	#    Warm clone: SKIPPED — mounting the disk would mutate it under the frozen
	#    RAM. The identity travels as MMDS metadata instead (step 4d); the
	#    in-guest freshen unit baked into the golden adopts it after resume (and
	#    on the cold-boot fallback, where the launcher preloads MMDS from the
	#    same file).
	#
	#    Boot-on-clone (spec/24 §0): SKIPPED here — identity was already injected
	#    THROUGH the clone device in the InjectingIdentity phase (mounting the plain
	#    LV would fault: it is held busy under the clone). Re-mounting now would also
	#    race the live read-through.
	if not warm and not boot_on_clone:
		inject_identity(
			disk.device_path,
			Identity(
				uuid=inputs.virtual_machine_name,
				ipv6_address=inputs.virtual_machine_ipv6,
				ssh_public_key=inputs.ssh_public_key,
				ipv4_guest_cidr=inputs.ipv4_guest_cidr,
				ipv4_gateway=inputs.ipv4_gateway,
				data_disk_mount_at=inputs.data_disk_mount_at,
				routing_base_url=inputs.routing_base_url,
				private_address=inputs.private_address,
			),
			# Birth of the VM: establish a fresh SSH host identity. The base image
			# ships SHARED baked host keys, and a clone seeds from another VM's
			# rootfs — both must be replaced so every VM is unique. (Rebuild/restore,
			# by contrast, preserve the disk's keys.) A migration cutover
			# (preserve_host_keys) also preserves them: the disk moved wholesale, so
			# its SSH identity must not change across hosts.
			regenerate_host_keys=not inputs.preserve_host_keys,
		)

	# 3. Kernel inside the jail. Hard-link (not copy) the immutable image kernel
	#    so we don't duplicate it per VM; same filesystem (/var/lib/atlas), so
	#    the link always succeeds. Read-only is fine for the jailed process.
	run("sudo ln -f {} {}", f"{image}/{inputs.kernel_filename}", paths.kernel)

	# 4. Firecracker config inside the jail, with jail-RELATIVE paths — they are
	#    resolved by the jailed process after chroot, so they are relative to the
	#    jail root (rootfs.ext4, vmlinux), not absolute host paths.
	install_file(_firecracker_config(inputs), paths.firecracker_config, mode="0644")

	# 4b. Expose the disk LV inside the jail as a block-special node at
	#     rootfs.ext4. firecracker.json's jail-relative `path_on_host:
	#     "rootfs.ext4"` (step 4) resolves to this node post-chroot — FC opens it
	#     as a plain block device, no config change from the file-backed era. The
	#     node is owned by the per-VM uid (chmod 0660); device access is pure
	#     DAC. The jailer never deletes existing nodes, so it survives every
	#     (re)start.
	#
	#     Boot-on-clone (spec/24 §0): point the jail node at the CLONE device, not
	#     the plain LV, so Firecracker reads through the clone (hydration serves
	#     un-copied blocks from the source over NBD). At CollapseClone the clone's
	#     table is reloaded to a linear map onto this same dest LV, keeping the SAME
	#     dm major:minor, so this node + Firecracker's open fd stay valid.
	if boot_on_clone:
		expose_device_in_jail(inputs.clone_rootfs_device, paths.rootfs_node, uid)
	else:
		disk.expose_in_jail(paths.rootfs_node, uid)

	# 4c. Expose the data disk inside the jail as a block node at data.ext4 — the
	#     guest's second drive (/dev/vdb). Same mknod/chown mechanism as the rootfs
	#     node; firecracker.json's `data` drive (path_on_host: "data.ext4") resolves
	#     to it post-chroot. Only when the VM has a data disk.
	#
	#     Boot-on-clone (spec/24 §0): expose the DATA clone device (its own dm-clone,
	#     the root clone's name with `-clone` → `-data-clone`) so /dev/vdb reads
	#     through until its own hydration completes, mirroring the root disk.
	if data_disk is not None:
		if boot_on_clone:
			data_clone_device = inputs.clone_rootfs_device.replace("-clone", "-data-clone")
			expose_device_in_jail(data_clone_device, paths.data_node, uid)
		else:
			data_disk.expose_in_jail(paths.data_node, uid)

	# 4d. Warm clone: stage this VM's identity as the MMDS payload. The guest
	#     can't learn its identity from the disk (step 2 was skipped), so the
	#     freshen unit baked into the golden reads it from the metadata service
	#     at 169.254.169.254 — vm-restore.py PUTs this file into MMDS before
	#     resuming, and the launcher preloads it (--metadata) on a cold boot.
	if warm:
		install_file(_mmds_metadata(inputs), paths.metadata_file, mode="0644")

	# 5. Hand the jail tree to the per-VM uid/gid. The jailer also chowns the
	#    jail root and the device nodes it creates, but the backing files we laid
	#    down (kernel RO, config) must be owned by the uid too. The recursive
	#    chown re-touches the rootfs.ext4 block node's inode (already uid-owned
	#    from step 4b) — correct and harmless; it chowns the node, not the LV it
	#    points at. Do this last, after every file is in place.
	run("sudo chown -R {} {}", f"{uid}:{uid}", paths.jail_chroot_base)

	# 5b. Warm clone: stage the golden memory pair behind a READY marker, AFTER
	#     the recursive chown — the pair is HARD-LINKED from the durable artifact
	#     (N clones CoW-share one read-only mem file; same filesystem, so ln
	#     always works), and a chown of the link would chown the shared inode
	#     itself. The inodes stay root-owned 0644 (any per-VM uid can map them);
	#     only the directory is handed to this VM's uid for traversal. The marker
	#     is written LAST — it asserts a complete, matching pair, exactly the
	#     same contract as snapshot-stop-vm.py's. vm-restore.py consumes the
	#     marker (only ever the marker: the link targets are shared) and checks
	#     the staged host signature before loading.
	if stage_warm:
		_stage_warm_pair(inputs.warm_snapshot_directory, paths, uid)
	elif warm:
		print("Disk LV already existed and was booted; staging no warm pair (next start cold-boots).")

	# 6. Sidecar that vm-network-up.sh reads. Stable across host reboots —
	#    carries the tap, address, and the per-VM netns + veth names so
	#    networking is reconstructible after a host reboot without consulting the
	#    Frappe DB.
	install_file(_network_env(inputs), paths.network_env, mode="0644")

	# 7. Per-VM launcher the systemd unit execs. We build the jailer command line
	#    HERE rather than inline in the unit's ExecStart because the --cgroup
	#    cpu.max value is "<quota> <period>" (an internal space the cgroup file
	#    format requires). systemd word-splits an unquoted $VAR in ExecStart on
	#    every space, which would shatter that value into a stray positional the
	#    jailer rejects ("Found argument '100000' ..."). The launcher is
	#    regenerated on every (re)provision, so it stays in sync with the row.
	#    `exec` so the jailer is the unit's main PID (KillMode=mixed). With a real
	#    arg vector the mapfile dance is gone: each cgroup/resource token is one
	#    pre-quoted line in the exec, internal spaces intact.
	install_file(_jailer_launch(inputs, paths), paths.jailer_launch, mode="0755")

	# 8. Enable and start the systemd unit. `enable` is instant (writes the
	#    multi-user.target.wants symlink) and stays synchronous. `start --no-block`
	#    queues the start job and returns immediately instead of blocking on the
	#    unit reaching active — which means waiting out network-online.target plus
	#    the two Python ExecStartPre hooks (vm-disk-up + vm-network-up, dozens of
	#    ip/nft calls). The controller marks the VM Running without waiting for boot
	#    anyway (VirtualMachine.provision), so nothing downstream needs the unit
	#    active by the time this Task returns. A failing ExecStartPre now surfaces
	#    async via the unit's own state (Restart=always); it is not lost.
	run("sudo systemctl enable {}", paths.systemd_unit)
	run("sudo systemctl start --no-block {}", paths.systemd_unit)

	print(f"Provisioned {inputs.virtual_machine_name}.")


def _guard_uid_collision(uid: int, own_rootfs_node: str) -> None:
	"""Fail loud if a *different* live VM's jail rootfs is already owned by this
	uid. Ports the shell's glob-and-stat loop. Our own node (idempotent re-run)
	is skipped. `stat -c %u` reads the owning uid; sudo because the jail tree is
	0700-owned by per-VM uids."""
	pattern = "/var/lib/atlas/virtual-machines/*/jail/firecracker/*/root/rootfs.ext4"
	for other_jail in glob.glob(pattern):
		if other_jail == own_rootfs_node:  # our own (idempotent re-run)
			continue
		owner = run("sudo stat -c %u {}", other_jail).strip()
		if owner == str(uid):
			sys.exit(f"uid {uid} already owned by {other_jail}; uid collision — terminate that VM or re-roll")


def _resolve_origin(inputs: "ProvisionInputs", pool: ThinPool):
	"""Resolve the origin LV the per-VM disk snapshots from. A snapshot LV wins
	(clone path); otherwise the base image LV. Same guards as rebuild —
	snapshot_rootfs_path is the snapshot's /dev/atlas/<name> device path."""
	if inputs.snapshot_rootfs_path:
		origin = pool.from_device(inputs.snapshot_rootfs_path)
		if not origin.exists:
			sys.exit(f"snapshot LV not found: {origin.name} (from {inputs.snapshot_rootfs_path})")
	else:
		origin = pool.base_image(inputs.image_name)
		if not origin.exists:
			sys.exit(f"base image LV not found: {origin.name}; run Sync to Server first")
	return origin


def _mmds_metadata(inputs: "ProvisionInputs") -> str:
	"""The MMDS payload for a warm clone: everything inject_identity would have
	written to the disk, served to the guest over the metadata service instead.
	The hostname/machine-id rules are Identity's own, so a warm clone's identity
	matches what a cold provision of the same UUID would get."""
	identity = Identity(
		uuid=inputs.virtual_machine_name,
		ipv6_address=inputs.virtual_machine_ipv6,
		ssh_public_key=inputs.ssh_public_key,
		ipv4_guest_cidr=inputs.ipv4_guest_cidr,
		ipv4_gateway=inputs.ipv4_gateway,
		routing_base_url=inputs.routing_base_url,
		private_address=inputs.private_address,
	)
	return (
		json.dumps(
			{
				"identity": {
					"uuid": identity.uuid,
					"hostname": identity.hostname,
					"machine_id": identity.machine_id,
					"ipv6": identity.ipv6_address,
					"ipv4_cidr": identity.ipv4_guest_cidr,
					"ipv4_gateway": identity.ipv4_gateway,
					"ssh_public_key": identity.ssh_public_key,
					# The routing base URL (spec/18); the freshen unit writes it to
					# /etc/atlas-routing.env when it adopts this clone's identity, the
					# warm-path analogue of rootfs._write_routing_identity. Empty for a
					# VM with no routing config — the guest client then no-ops.
					"routing_base_url": identity.routing_base_url,
					# The private-plane /128 (spec/25) — per-clone (derived from the
					# clone's UUID), so it MUST ride MMDS like the ipv6/ipv4 do. The
					# freshen unit writes it into /etc/atlas-network.env so the warm
					# clone joins the mesh on boot. Empty for a VM off the plane.
					"private_address": identity.private_address,
				}
			},
			indent=1,
		)
		+ "\n"
	)


def _stage_warm_pair(warm_snapshot_directory: str, paths: VirtualMachinePaths, uid: int) -> None:
	"""Hard-link the durable golden pair into the clone jail and arm the marker."""
	install_directory(paths.memory_snapshot_directory, mode="0700")
	run("sudo chown {} {}", f"{uid}:{uid}", paths.memory_snapshot_directory)
	for name in ("vmstate.bin", "mem.bin"):
		source = f"{warm_snapshot_directory}/{name}"
		if not run_ok("sudo test -s {}", source):
			sys.exit(f"warm snapshot file missing or empty: {source}; re-bake the warm golden")
		run("sudo ln -f {} {}", source, f"{paths.memory_snapshot_directory}/{name}")
	run("sudo cp {} {}", f"{warm_snapshot_directory}/host-signature.json", paths.memory_snapshot_signature)
	run("sudo touch {}", paths.memory_snapshot_marker)


def _firecracker_config(inputs: "ProvisionInputs") -> str:
	"""The jail's firecracker.json. Built from a dict + json.dumps for
	cleanliness; the boot_args / drives / network-interfaces / machine-config
	shape is identical to the shell heredoc, with jail-RELATIVE host paths
	(vmlinux, rootfs.ext4) resolved post-chroot."""
	config = {
		"boot-source": {
			"kernel_image_path": "vmlinux",
			# 8250.nr_uarts=0 disables the guest 8250 serial device at boot
			# (prod-host-setup.md "8250 Serial Device"): the device is tied to
			# Firecracker's stdout, and a guest with serial access can drive
			# unbounded host log/storage growth. We do NOT pass `console=ttyS0` —
			# the guest's console writes would otherwise flood firecracker.log. The
			# host side is bounded too (the systemd unit logrotates the per-VM log);
			# the guest can technically re-enable the device after boot, so the
			# bounded-storage half is the load-bearing mitigation. reboot=k / panic=1
			# keep the guest's reboot+panic behaviour unchanged.
			"boot_args": "8250.nr_uarts=0 reboot=k panic=1",
		},
		"drives": [
			{
				"drive_id": "rootfs",
				"path_on_host": "rootfs.ext4",
				"is_root_device": True,
				"is_read_only": False,
			}
		],
		"network-interfaces": [
			{
				"iface_id": "eth0",
				"guest_mac": inputs.mac_address,
				"host_dev_name": inputs.tap_device,
			}
		],
		"machine-config": {
			"vcpu_count": inputs.vcpus,
			"mem_size_mib": inputs.memory_mb,
		},
		# The metadata service, on every VM. Inert unless something PUTs data
		# (warm clones stage their identity payload; ordinary VMs serve nothing)
		# — but it must be in the GOLDEN's boot config for the captured vmstate
		# to carry the MMDS-enabled net device, and a uniform config keeps every
		# VM bakeable. V1 pinned: the freshen unit does a plain GET, no session
		# tokens.
		"mmds-config": {
			"version": "V1",
			"network_interfaces": ["eth0"],
		},
		# virtio-rng: feed the guest kernel a hardware RNG so it seeds its CSPRNG
		# from the host's AWS-LC entropy. Ubuntu ships CONFIG_HW_RANDOM_VIRTIO=m, so
		# the driver isn't in the extracted vmlinux — sync-image.py bakes the
		# virtio_rng module into the rootfs and pins it in modules-load.d, and only
		# then does the device bind (/dev/hwrng). No rate_limiter — entropy is cheap
		# and we want it as fast as the guest asks. Like mmds-config, it must be in
		# the GOLDEN's boot config so the captured vmstate carries the device.
		"entropy": {},
	}
	# The data disk is a second, non-root drive (the guest's /dev/vdb), resolved
	# post-chroot to the data.ext4 block node exposed in step 4c. Only when the VM
	# has a data disk, so an ordinary VM's config is byte-identical to before.
	if inputs.data_disk_gb > 0:
		config["drives"].append(
			{
				"drive_id": "data",
				"path_on_host": "data.ext4",
				"is_root_device": False,
				"is_read_only": False,
			}
		)
	return json.dumps(config, indent=2) + "\n"


def _network_env(inputs: "ProvisionInputs") -> str:
	"""The network.env sidecar vm-network-up.sh reads, byte-shape identical to
	the shell heredoc."""
	env = (
		f"TAP_DEVICE={inputs.tap_device}\n"
		f"VIRTUAL_MACHINE_IPV6={inputs.virtual_machine_ipv6}\n"
		f"ATLAS_NETNS={inputs.atlas_netns}\n"
		f"HOST_VETH={inputs.host_veth}\n"
		f"NAMESPACE_VETH={inputs.namespace_veth}\n"
		f"IPV4_HOST_CIDR={inputs.ipv4_host_cidr}\n"
		f"IPV4_GUEST_CIDR={inputs.ipv4_guest_cidr}\n"
		f"ATLAS_FC_UID={inputs.atlas_fc_uid}\n"
	)
	# Only written when the VM has a Reserved IP attached — vm-network-up reads it
	# with .get() and skips the 1:1-NAT block when absent, so an ordinary VM's env
	# is unchanged.
	if inputs.reserved_ipv4:
		env += f"RESERVED_IPV4={inputs.reserved_ipv4}\n"
	# The private-plane identity (§5). Written together (vm-network-up gates the
	# private block on BOTH), only when the VM has a tenant. Absent on a pre-feature
	# or tenant-less VM, so the private block is a complete no-op there.
	if inputs.private_address and inputs.tenant_prefix:
		env += f"PRIVATE_ADDRESS={inputs.private_address}\n"
		env += f"TENANT_PREFIX={inputs.tenant_prefix}\n"
	return env


def _jailer_launch(inputs: "ProvisionInputs", paths: VirtualMachinePaths) -> str:
	"""The per-VM jailer-launch.sh the systemd unit execs. cgroup_args /
	resource_args arrive as lists (one argv token each, from the repeatable
	--cgroup-arg / --resource-arg flags); we expand each as its own
	backslash-continued line in the exec, so a token with an internal space
	(cpu.max's "<quota> <period>") stays one argument — no mapfile, no systemd
	word-splitting.

	CRITICAL: each token MUST be shlex.quote()'d, or bash would word-split
	"cpu.max=100000 100000" back into two arguments in this generated `exec`
	line — re-introducing the exact "Found argument '100000'" bug the shell's
	mapfile + "${cgroup_args[@]}" quoting existed to prevent."""
	jailer_lines = [
		"exec /usr/local/bin/jailer \\",
		f"    --id {inputs.virtual_machine_name} \\",
		"    --exec-file /usr/local/bin/firecracker \\",
		f"    --uid {inputs.atlas_fc_uid} \\",
		f"    --gid {inputs.atlas_fc_uid} \\",
		"    --cgroup-version 2 \\",
		f"    --netns /var/run/netns/{inputs.atlas_netns} \\",
	]
	# Interleave the constant flag with each value on its own continued line,
	# value shlex.quote()'d so an internal space (cpu.max's "<quota> <period>")
	# survives bash word-splitting — the Python equivalent of the shell's
	# "${cgroup_args[@]}" expansion, with the --cgroup/--resource-limit prefix
	# owned here instead of carried in the input.
	for value in inputs.cgroup_arg:
		jailer_lines.append(f"    --cgroup {shlex.quote(value)} \\")
	# resource_arg defaults to [] (an operator omitting the flag on a hand run); fall
	# back to the constant no-file limit so the jail is still descriptor-bounded. The
	# controller always passes a non-empty set, so its path is unaffected.
	for value in inputs.resource_arg or DEFAULT_RESOURCE_ARGS:
		jailer_lines.append(f"    --resource-limit {shlex.quote(value)} \\")
	jailer_lines += [
		f"    --chroot-base-dir {paths.jail_chroot_base} \\",
		"    -- \\",
		"    --api-sock run/firecracker.socket \\",
		'    "${boot_args[@]}"',
	]
	exec_block = "\n".join(jailer_lines)
	return (
		"#!/bin/bash\n"
		f"# GENERATED by provision-vm.py for VM {inputs.virtual_machine_name}. "
		"Do not edit.\n"
		"set -euo pipefail\n"
		"\n"
		"# Cold boot passes --config-file. When a complete memory snapshot is\n"
		"# pending (marker written by snapshot-stop-vm.py, or staged from a warm\n"
		"# golden by provision-vm.py), Firecracker must start IDLE instead —\n"
		"# /snapshot/load is pre-boot only and cannot coexist with --config-file.\n"
		"# vm-restore.py (ExecStartPost) then loads and resumes it. A warm clone's\n"
		"# staged MMDS payload (metadata.json) rides --metadata on the cold path\n"
		"# (so the cold-boot FALLBACK still adopts the clone identity); on the\n"
		"# idle path vm-restore.py PUTs it over the API instead.\n"
		"boot_args=(--config-file firecracker.json)\n"
		f"if [[ -f {paths.metadata_file} ]]; then\n"
		"    boot_args+=(--metadata metadata.json)\n"
		"fi\n"
		f"if [[ -f {paths.memory_snapshot_marker} ]]; then\n"
		"    boot_args=()\n"
		"fi\n"
		"\n"
		f"{exec_block}\n"
	)


if __name__ == "__main__":
	main()
