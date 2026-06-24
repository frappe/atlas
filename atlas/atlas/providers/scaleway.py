"""Scaleway Elastic Metal provider implementation.

Reads `Scaleway Settings` for the secret key / zone / project / billing /
defaults, and `Atlas Settings` (via `atlas.get_ssh_key()`) for the SSH key
body — Scaleway is a "uploads the key at provision time" vendor, so it
registers the public key with IAM and references the returned id in the
install. Delegates HTTP to `atlas.atlas.scaleway.ScalewayClient`.

Two ways Scaleway differs from DigitalOcean, both already accommodated by the
abstraction:

- **Async, two-phase provision.** `create_server` returns immediately with
  `status="delivering"`; the OS install (`install` sub-object, one-call deploy)
  runs after. `describe()` is `ready` only when `status=="ready"` AND
  `install.status=="completed"`, so the worker's poll loop carries this.
- **Routed /64, no carve.** Scaleway routes the whole /64 to the host (no DO
  edge /124 limit), so `ipv6_virtual_machine_range` is the full /64 — we do NOT
  call `carve_virtual_machine_range`. This retires the 15-VM ceiling.

`discover()` *does* hit the API (unlike DO's hand-maintained constants): the
offer/OS endpoints are the only source of the per-zone `offer_id` / `os_id`
UUIDs the create/install calls need, and prices/stock legitimately vary by
zone. The UUIDs are stashed in each Provider Size / Provider Image row's
`provider_metadata` so `provision()` resolves them without a live lookup.
"""

from __future__ import annotations

import frappe

from atlas.atlas.providers import register
from atlas.atlas.providers.base import (
	AuthResult,
	Capabilities,
	DiscoveredServer,
	ImageInfo,
	Provider,
	ProviderError,
	ProvisionRequest,
	ProvisionResult,
	ReservedIp,
	ServerNetworking,
	SizeInfo,
)
from atlas.atlas.scaleway import (
	TERMINAL_SERVER_STATUSES,
	ScalewayClient,
	ScalewayError,
	flexible_ip_address,
	flexible_ip_server_id,
	public_ipv4,
	public_ipv6,
)
from atlas.atlas.secrets import get_secret

# Bare metal installs can take up to ~1h worst case (vs a droplet's seconds),
# so the worker waits longer for a Scaleway server than the 600s default.
READY_TIMEOUT_SECONDS = 3600

# Fixed-size RAID-1 members (bytes). Everything else on the disk falls to the
# `data` partition (use_all_available_space), which becomes the LVM pool PV.
_EFI_SIZE_BYTES = 512 * 1024 * 1024  # 512 MiB ESP (only disk 0's is mounted)
_BOOT_SIZE_BYTES = 1024 * 1024 * 1024  # 1 GiB /boot
_ROOT_SIZE_BYTES = 64 * 1024 * 1024 * 1024  # 64 GiB / (raid1)
_RAID_LEVEL_1 = "raid_level_1"


@register
class ScalewayProvider(Provider):
	provider_type = "Scaleway"
	ready_timeout_seconds = READY_TIMEOUT_SECONDS

	def __init__(self) -> None:
		settings = frappe.get_single("Scaleway Settings")
		secret_key = get_secret("Scaleway Settings", "Scaleway Settings", "secret_key")
		self.zone = settings.zone
		self.project_id = settings.project_id
		self.organization_id = settings.organization_id or None
		self.billing = settings.billing or "hourly"
		self.default_size = settings.default_size
		self.default_image = settings.default_image
		self.client = ScalewayClient(secret_key=secret_key, zone=self.zone)

	def authenticate(self) -> AuthResult:
		try:
			result = self.client.verify_credentials(self.organization_id)
		except ScalewayError as exception:
			return AuthResult(ok=False, error=str(exception))
		return AuthResult(ok=True, account_label=result.get("account_label"))

	def discover(self) -> Capabilities:
		offers = self.client.list_offers(subscription_period=self.billing)
		sizes = tuple(_size_from_offer(offer) for offer in offers)
		images = tuple(_image_from_os(os_image) for os_image in self.client.list_os())
		return Capabilities(sizes=sizes, images=images)

	def provision(self, request: ProvisionRequest) -> ProvisionResult:
		offer_id = self._resolve_offer_id(request.size)
		os_id = self._resolve_os_id(request.image)
		ssh_key_id = self._ensure_ssh_key(request)
		install = {
			"os_id": os_id,
			"hostname": request.title,
			"ssh_key_ids": [ssh_key_id] if ssh_key_id else [],
		}
		# Drive the install with an explicit partitioning schema: symmetric, aligned
		# disks (boot + 64 GiB root mirrored RAID-1, the rest a RAID-1 `data` array
		# left raw for the LVM thin pool). Without this Scaleway's default lands
		# boot/root on disk 0 and leaves disk 1 inconsistent, so the pool backing is
		# non-deterministic. Fetch the vendor default (the source of truth for THIS
		# box's real device names) and rewrite it.
		schema = self._build_partitioning_schema(offer_id, os_id)
		if schema is not None:
			install["partitioning_schema"] = schema
		server = self.client.create_server(
			name=request.title,
			offer_id=offer_id,
			project_id=self.project_id,
			tags=list(request.tags),
			install=install,
			user_data=request.cloud_init,
		)
		server_id = str(server["id"])
		# The bundled /64 arrives on-link (SLAAC), NOT routed to the host — so it
		# is the host's own subnet, not a VM range. The routed /64 VMs need is a
		# (free) flexible IPv6, which Scaleway's edge routes to the host's link;
		# per-VM /128 routes + proxy-NDP do the rest (proven on the live atlas-1
		# Scaleway host). Allocate + attach one now; describe() reports it as the
		# ipv6_virtual_machine_range once the host is ready.
		self._ensure_flexible_ipv6(server_id)
		return ProvisionResult(
			provider_resource_id=server_id,
			size=request.size,
			image=request.image,
			ready=False,
			networking=None,
			provider_metadata=server,
		)

	def describe(self, provider_resource_id: str) -> ProvisionResult:
		server = self.client.get_server(provider_resource_id)
		status = server.get("status")
		if status in TERMINAL_SERVER_STATUSES:
			raise ProviderError(f"Scaleway server {provider_resource_id} is {status!r}")
		install = server.get("install") or {}
		ready = status == "ready" and install.get("status") == "completed"
		size_name = f"{self.provider_type}/{server.get('offer_name')}" if server.get("offer_name") else ""
		image_name = (
			f"{self.provider_type}/{_os_slug_from_install(install)}" if _os_slug_from_install(install) else ""
		)
		if not ready:
			return ProvisionResult(
				provider_resource_id=provider_resource_id,
				size=size_name,
				image=image_name,
				ready=False,
				networking=None,
				provider_metadata=server,
			)
		ipv4 = public_ipv4(server)
		ipv6_address, ipv6_prefix = public_ipv6(server)
		# The host's own /128 + bundled /64 come from the on-link subnet; the VM
		# range is the ROUTED flexible /64 attached at provision (no carve — a /64
		# is effectively unbounded, retiring the DO 15-VM /124 ceiling). Find it on
		# the server, falling back to the bundled prefix if absent (so a host that
		# predates the flexible-v6 allocation still describes).
		vm_range = self._flexible_ipv6_range(provider_resource_id) or ipv6_prefix
		networking = ServerNetworking(
			ipv4_address=ipv4,
			ipv6_address=ipv6_address,
			ipv6_prefix=ipv6_prefix,
			ipv6_virtual_machine_range=vm_range,
		)
		return ProvisionResult(
			provider_resource_id=provider_resource_id,
			size=size_name,
			image=image_name,
			ready=True,
			networking=networking,
			provider_metadata=server,
		)

	def destroy(self, provider_resource_id: str) -> None:
		# Release the flexible IPs the server holds first — the v6 VM-range block
		# (allocated at provision) and any v4 FIPs would otherwise leak (v4 is
		# billable even while detached). Idempotent: a missing FIP 404s through.
		for fip in self.client.list_flexible_ips():
			if str(fip.get("server_id")) == str(provider_resource_id):
				self.client.delete_flexible_ip(str(fip["id"]))
		self.client.delete_server(provider_resource_id)

	def list_servers(self) -> tuple[DiscoveredServer, ...]:
		"""Every Elastic Metal server in the zone, for discover/import. The size
		label mirrors describe()'s `Scaleway/<offer_name>` form so the preview row
		reads like the rest of the catalog. IPv4 is best-effort (a box still
		delivering may have none yet — describe() is the authority at import)."""
		return tuple(
			_discovered_from_server(self.provider_type, server) for server in self.client.list_servers()
		)

	def prepare_host(self, server) -> None:
		"""First contact: Scaleway's Ubuntu image force-blocks root SSH (the
		cloud-image forced-command on root's authorized_keys), so we SSH in as
		`ubuntu` (passwordless sudo), copy its authorized_keys to /root and strip
		any forced-command prefix. After this the rest of Atlas reaches the host
		as root unchanged. Idempotent — re-running just overwrites /root's keys.

		Waits for the `ubuntu`-user SSH first (sshd can lag the vendor `ready`
		state), so this absorbs the post-install boot, not the later root-SSH
		wait (which would otherwise time out against blocked root)."""
		import atlas
		from atlas.atlas._ssh.transport import Connection, run_ssh, ssh_key_file, wait_for_ssh
		from atlas.atlas.secrets import get_ssh_key_from_disk

		if not server.ipv4_address:
			raise ProviderError(f"Server {server.name} has no ipv4_address; cannot prepare host")
		key_path = atlas.get_ssh_private_key_path()
		connection = Connection(
			host=server.ipv4_address,
			ssh_private_key=get_ssh_key_from_disk(key_path),
			user="ubuntu",
		)
		wait_for_ssh(connection, timeout_seconds=300)
		# Copy ubuntu's authorized_keys to root and strip the cloud-image
		# forced-command (`command="…",no-port-forwarding,… ssh-… key`) so the
		# bare key is left — `sed 's/.*ssh-/ssh-/'` keeps from the first key type.
		enable_root = (
			"set -e; "
			"sudo install -m 0700 -d /root/.ssh; "
			"sudo cp /home/ubuntu/.ssh/authorized_keys /root/.ssh/authorized_keys; "
			"sudo sed -i 's/.*ssh-/ssh-/' /root/.ssh/authorized_keys; "
			"sudo chmod 600 /root/.ssh/authorized_keys; "
			"sudo chown root:root /root/.ssh/authorized_keys"
		)
		with ssh_key_file(connection.ssh_private_key) as resolved_key_path:
			_, stderr, code = run_ssh(connection, resolved_key_path, enable_root, timeout_seconds=60)
		if code != 0:
			raise ProviderError(f"Scaleway first-contact root-enable failed (exit {code}): {stderr[-300:]}")

	# --- Reserved IPs (Flexible IP) --------------------------------------
	# Scaleway keys a flexible IP by its own UUID (unlike DO, where the address
	# IS the handle). So `provider_resource_id` is the FIP id, not the address;
	# `droplet_resource_id` is the attached server's id.

	def allocate_reserved_ip(self) -> ReservedIp:
		fip = self.client.create_flexible_ip(project_id=self.project_id, is_ipv6=False)
		return _reserved_ip_from_payload(fip)

	def assign_reserved_ip(self, provider_resource_id: str, droplet_resource_id: str) -> None:
		self.client.attach_flexible_ip(provider_resource_id, droplet_resource_id)

	def unassign_reserved_ip(self, provider_resource_id: str) -> None:
		self.client.detach_flexible_ip(provider_resource_id)

	def list_reserved_ips(self) -> tuple[ReservedIp, ...]:
		# The Reserved IP primitive is inbound-v4 only; v6 flexible IPs are VM
		# ranges (allocated at provision), not reserved IPs — skip them so the
		# pool import doesn't mistake a /64 block for a reserved v4.
		return tuple(
			_reserved_ip_from_payload(fip) for fip in self.client.list_flexible_ips() if not _is_ipv6_fip(fip)
		)

	def release_reserved_ip(self, provider_resource_id: str) -> None:
		self.client.delete_flexible_ip(provider_resource_id)

	# --- helpers ---------------------------------------------------------

	def _resolve_offer_id(self, size: str) -> str:
		"""Read the vendor offer_id stashed in the Provider Size row's metadata."""
		offer_id = _metadata_value("Provider Size", size, "offer_id")
		if not offer_id:
			frappe.throw(f"Provider Size {size!r} has no offer_id; run Refresh Catalog")
		return offer_id

	def _resolve_os_id(self, image: str) -> str:
		os_id = _metadata_value("Provider Image", image, "os_id")
		if not os_id:
			frappe.throw(f"Provider Image {image!r} has no os_id; run Refresh Catalog")
		return os_id

	def _ensure_ssh_key(self, request: ProvisionRequest) -> str | None:
		"""Return the IAM SSH key id to install, reusing an existing key for the
		Atlas keypair rather than registering a fresh one on every provision.

		Order: (1) the cached vendor_id (`Scaleway Settings.ssh_key_id`); (2) an
		IAM key already registered with a matching body — Atlas is one-key, so a
		prior provision (or a manual upload) leaves the key in IAM and re-running
		would otherwise pile up duplicate records; (3) register it once and return
		the new id. The operator caches the id on Scaleway Settings once known, so
		the steady state is path (1)."""
		if not (request.ssh_key and request.ssh_key.public_key):
			return request.ssh_key.vendor_id if request.ssh_key else None
		if request.ssh_key.vendor_id:
			return request.ssh_key.vendor_id
		existing = self._find_ssh_key_id(request.ssh_key.public_key)
		if existing:
			return existing
		created = self.client.register_ssh_key(
			name=request.title,
			public_key=request.ssh_key.public_key,
			project_id=self.project_id,
		)
		return str(created["id"])

	def _find_ssh_key_id(self, public_key: str) -> str | None:
		"""The id of an IAM key in the project whose body matches `public_key`, or
		None. Matched on the `<type> <base64>` core (first two tokens) so a
		differing trailing comment — IAM keeps/derives its own — doesn't miss the
		match."""
		wanted = _ssh_key_identity(public_key)
		if not wanted:
			return None
		for key in self.client.list_ssh_keys(self.project_id):
			if _ssh_key_identity(key.get("public_key") or "") == wanted:
				return str(key["id"])
		return None

	def _build_partitioning_schema(self, offer_id: str, os_id: str) -> dict | None:
		"""Fetch the vendor default partitioning schema for this offer+OS and rewrite
		it into the RAID-1 layout (see `build_raid_partitioning_schema`). Returns None
		— so the install falls back to Scaleway's default — if the offer/OS does not
		support custom partitioning (the endpoint 404s) or exposes fewer than two
		disks (RAID-1 needs a mirror pair). A bad-shape default raises through, since
		that is an Atlas/vendor contract change worth surfacing, not silently ignoring."""
		try:
			default = self.client.get_default_partitioning_schema(offer_id, os_id)
		except ScalewayError as exception:
			# Offer/OS without custom partitioning (or a transient 4xx): fall back to
			# the vendor default install rather than failing the whole provision.
			frappe.logger().info(
				f"Scaleway default partitioning unavailable ({exception}); using vendor default"
			)
			return None
		devices = [disk.get("device") for disk in (default.get("disks") or []) if disk.get("device")]
		if len(devices) < 2:
			frappe.logger().info(
				f"Scaleway offer exposes {len(devices)} disk(s); RAID-1 needs ≥2 — using vendor default"
			)
			return None
		return build_raid_partitioning_schema(devices)

	def _ensure_flexible_ipv6(self, server_id: str) -> str:
		"""Allocate + attach a (free) flexible IPv6 /64 to the server, returning
		its /64 CIDR. Idempotent: if the server already holds a v6 flexible IP,
		reuse it rather than stacking a second. The attach is async at the vendor;
		the host picks the routed /64 up via Scaleway's edge (no host-side
		hot-plug needed — per-VM routes + proxy-NDP carry it)."""
		existing = self._flexible_ipv6_range(server_id)
		if existing:
			return existing
		fip = self.client.create_flexible_ip(project_id=self.project_id, is_ipv6=True)
		self.client.attach_flexible_ip(str(fip["id"]), server_id)
		return _flexible_ipv6_cidr(fip)

	def _flexible_ipv6_range(self, server_id: str) -> str | None:
		"""The /64 CIDR of the v6 flexible IP attached to `server_id`, or None.
		v4 flexible IPs (the inbound-v4 Reserved IP primitive) are skipped — only
		a v6 block is a VM range."""
		for fip in self.client.list_flexible_ips():
			if str(fip.get("server_id")) == server_id and _is_ipv6_fip(fip):
				return _flexible_ipv6_cidr(fip)
		return None


def build_raid_partitioning_schema(devices: list[str]) -> dict:
	"""Build a symmetric RAID-1 `partitioning_schema` across the first two disks.

	The rules (operator spec): both disks get an IDENTICAL partition table so the
	partition numbers line up across them — including a `uefi` partition on the
	second disk that is pure buffer (only disk 0's ESP is mounted), matching what
	Scaleway otherwise puts only on disk 0. Each disk carries:

	  p1 uefi  512 MiB   — ESP on disk 0; an aligning buffer on disk 1
	  p2 boot  1 GiB     — RAID-1 → /dev/md0 → /boot
	  p3 root  64 GiB    — RAID-1 → /dev/md1 → /
	  p4 data  rest      — RAID-1 → /dev/md2 → raw, becomes the LVM thin-pool PV

	`devices` are the box's real disk paths (e.g. /dev/nvme0n1), taken from the
	vendor's default schema so this adapts to the actual hardware. Only the first
	two are mirrored; any extra disks are left untouched by the installer. The
	`data` RAID (md2) is deliberately ABSENT from `filesystems` — left raw so
	`ThinPool` consumes it as the PV (`discover_pool_disks` picks the unused md
	array). Pure: device list in, schema dict out — unit-testable with no API."""
	disk0, disk1 = devices[0], devices[1]

	def _table(device: str) -> dict:
		return {
			"device": device,
			"partitions": [
				{"label": "uefi", "number": 1, "size": _EFI_SIZE_BYTES},
				{"label": "boot", "number": 2, "size": _BOOT_SIZE_BYTES},
				{"label": "root", "number": 3, "size": _ROOT_SIZE_BYTES},
				{"label": "data", "number": 4, "use_all_available_space": True},
			],
		}

	def _part(device: str, number: int) -> str:
		# nvme drives number partitions /dev/nvme0n1p1; sd/vd drives /dev/sda1. The
		# vendor default uses the same `<device>p<n>` form for nvme members, which is
		# what Elastic Metal exposes — derive it off the trailing digit either way.
		separator = "p" if device[-1].isdigit() else ""
		return f"{device}{separator}{number}"

	raids = [
		{"name": "/dev/md0", "level": _RAID_LEVEL_1, "devices": [_part(disk0, 2), _part(disk1, 2)]},
		{"name": "/dev/md1", "level": _RAID_LEVEL_1, "devices": [_part(disk0, 3), _part(disk1, 3)]},
		{"name": "/dev/md2", "level": _RAID_LEVEL_1, "devices": [_part(disk0, 4), _part(disk1, 4)]},
	]
	filesystems = [
		{"device": _part(disk0, 1), "format": "fat32", "mountpoint": "/boot/efi"},
		{"device": "/dev/md0", "format": "ext4", "mountpoint": "/boot"},
		{"device": "/dev/md1", "format": "ext4", "mountpoint": "/"},
		# /dev/md2 (data) intentionally omitted — raw block device for the LVM pool.
	]
	return {"disks": [_table(disk0), _table(disk1)], "raids": raids, "filesystems": filesystems}


def _discovered_from_server(provider_type: str, server: dict) -> DiscoveredServer:
	"""Map a raw Scaleway server payload to a DiscoveredServer for the picker.
	The size label mirrors describe()'s `<provider_type>/<offer_name>` form; the
	IPv4 is best-effort (a delivering box may have none yet, and `public_ipv4`
	raises in that case — discovery must not break on one v4-less box)."""
	offer_name = server.get("offer_name")
	size = f"{provider_type}/{offer_name}" if offer_name else None
	try:
		ipv4 = public_ipv4(server)
	except ScalewayError:
		ipv4 = None
	return DiscoveredServer(
		provider_resource_id=str(server["id"]),
		title=server.get("name") or None,
		ipv4_address=ipv4,
		size=size,
		provider_metadata=server,
	)


def _size_from_offer(offer: dict) -> SizeInfo:
	"""Map a Scaleway offer to a SizeInfo. The slug is the human offer name
	(e.g. EM-A610R-NVMe); the per-zone offer_id UUID and the raw offer (cpu/ram/
	disk/price/stock) go into provider_metadata so provision() resolves the id."""
	# Hourly offers carry no `price_per_month` (it is null) — the shared
	# `monthly_cost_usd` column is a NOT-NULL Int, so coerce a missing monthly
	# price to 0 ("n/a on hourly"). The hourly rate lives in provider_metadata.
	price = offer.get("price_per_month") or {}
	monthly_cost = _money_to_int(price) or 0
	metadata = dict(offer)
	metadata["offer_id"] = offer.get("id")
	return SizeInfo(
		slug=offer.get("name"),
		monthly_cost_usd=monthly_cost,
		provider_metadata=metadata,
	)


def _image_from_os(os_image: dict) -> ImageInfo:
	"""Map a Scaleway OS to an ImageInfo. Slug is `<name>_<version>` (e.g.
	Ubuntu_24.04); the per-zone os_id UUID goes into provider_metadata.

	Scaleway's `version` carries the marketing name too ("24.04 LTS (Noble
	Numbat)"); we keep only the leading version token so the slug — which is the
	operator-facing Provider Image handle and the `atlas_scw_image` config value
	— stays terse. The full raw version stays in provider_metadata."""
	name = os_image.get("name") or ""
	version = (os_image.get("version") or "").split()[0] if os_image.get("version") else ""
	slug = f"{name}_{version}".strip("_").replace(" ", "_")
	metadata = dict(os_image)
	metadata["os_id"] = os_image.get("id")
	return ImageInfo(slug=slug, provider_metadata=metadata)


def _ssh_key_identity(public_key: str) -> str:
	"""The comment-agnostic identity of an SSH public key: `<type> <base64>` (the
	first two whitespace tokens). Two keys with the same body but different
	trailing comments compare equal; empty/garbage input yields `""`."""
	parts = (public_key or "").split()
	return " ".join(parts[:2]) if len(parts) >= 2 else ""


def _is_ipv6_fip(fip: dict) -> bool:
	"""True if a flexible IP is a v6 block. The API's `is_ipv6` flag comes back
	null in list responses, so fall back to the address family — a v6 address
	always contains a colon, a v4 one never does."""
	if fip.get("is_ipv6") is True:
		return True
	return ":" in (fip.get("ip_address") or fip.get("address") or "")


def _flexible_ipv6_cidr(fip: dict) -> str:
	"""The /64 CIDR for a v6 flexible IP. Scaleway returns `ip_address` already as
	`<prefix>::/64`; normalise to the network form (default /64 if bare)."""
	import ipaddress

	address = fip.get("ip_address") or fip.get("address") or ""
	if "/" not in address:
		address = f"{address}/64"
	return str(ipaddress.IPv6Network(address, strict=False))


def _reserved_ip_from_payload(fip: dict) -> ReservedIp:
	return ReservedIp(
		ip_address=flexible_ip_address(fip),
		provider_resource_id=str(fip["id"]),
		droplet_resource_id=flexible_ip_server_id(fip),
		provider_metadata=fip,
	)


def _money_to_int(money: dict) -> int | None:
	"""Scaleway Money → integer major units. Money is {currency_code, units,
	nanos}; we round to whole units for the monthly_cost field (EUR, stored in a
	field named for USD — currency is noted in provider_metadata)."""
	if not money:
		return None
	units = money.get("units")
	if units is None:
		return None
	nanos = money.get("nanos") or 0
	return round(units + nanos / 1_000_000_000)


def _metadata_value(doctype: str, name: str, key: str) -> str | None:
	"""Read one key out of a catalog row's provider_metadata JSON."""
	import json

	raw = frappe.db.get_value(doctype, name, "provider_metadata")
	if not raw:
		return None
	try:
		return json.loads(raw).get(key)
	except (ValueError, TypeError):
		return None


def _os_slug_from_install(install: dict) -> str | None:
	"""Best-effort image slug from the persisted install object — Scaleway
	returns os_id (a UUID), not a name, so there is no stable slug to surface.
	We leave the image name to what provision() recorded; describe() does not
	overwrite it with a UUID."""
	return None
