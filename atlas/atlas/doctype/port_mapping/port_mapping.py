import frappe
from frappe import _
from frappe.model.document import Document

# The routing key is the allocated port (read-only) and the target VM + service
# port are fixed once chosen — repointing a live mapping at a different VM/port is
# a delete-and-recreate, not an in-place edit, so the proxy map change is explicit
# (the same discipline as Subdomain). `public_port` is allocated by Atlas, so it
# is read-only rather than listed here.
IMMUTABLE_AFTER_INSERT = (
	"region",
	"virtual_machine",
	"target_port",
)

# The Atlas Settings field holding the per-region TCP port pool, and its default
# (matches the proxy's pre-opened `listen 10000-19999;` range, spec/17-tcp-proxy.md).
PORT_POOL_FIELD = "tcp_port_pool"
DEFAULT_PORT_POOL = "10000-19999"


class PortMapping(Document):
	def before_insert(self) -> None:
		"""Allocate the public port: the lowest port in the region's pool not
		already held by an active OR inactive mapping in the region. Runs before
		set_new_name so the `{region}-{public_port}` autoname picks it up."""
		self.public_port = allocate_port(self.region)

	def validate(self) -> None:
		self._validate_immutability()
		self._denormalize_address()

	def after_insert(self) -> None:
		"""Auto-reconcile: a new active mapping changes the region's served port
		map, so push it to the fleet — the operator never runs a reconcile by hand
		after creating a mapping (mirrors Subdomain.after_insert)."""
		self._enqueue_reconcile()

	def on_update(self) -> None:
		"""`public_port`, `region`, `virtual_machine`, and `target_port` are all
		read-only/immutable, so `active` is the only mutable field that changes the
		served map. Reconcile only when it actually flipped."""
		original = self.get_doc_before_save()
		if original and original.active != self.active:
			self._enqueue_reconcile()

	def on_trash(self) -> None:
		"""Deleting an active mapping drops it from the served map; reconcile so the
		proxy fleet stops forwarding the port."""
		self._enqueue_reconcile()

	def _enqueue_reconcile(self) -> None:
		"""Background-reconcile this mapping's region. Region-deduplicated and
		after-commit, byte-for-byte the same idiom as Subdomain._enqueue_reconcile
		(a reconcile reads the WHOLE region's desired map, so it is the same job no
		matter which mapping triggered it — N changes need one reconcile, not N;
		without dedup a burst floods `long` and a wedged proxy makes each take its
		full SSH timeout). queue=long because the job SSHes into every proxy in the
		region; tcp_reconcile_region tolerates an empty fleet and isolates per-proxy
		failures, so a missing or wedged proxy never fails the operator's save."""
		frappe.enqueue(
			"atlas.atlas.doctype.port_mapping.port_mapping.tcp_reconcile_region",
			queue="long",
			timeout=300,
			job_id=f"tcp_reconcile_region::{self.region}",
			deduplicate=True,
			enqueue_after_commit=True,
			region=self.region,
		)

	def _validate_immutability(self) -> None:
		"""Lock the region, target VM, and service port once written. `public_port`
		is read-only (allocated), and `active` toggles the mapping in/out of the
		served map."""
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			if getattr(original, field) != getattr(self, field):
				frappe.throw(f"{field} is immutable after insert")

	def _denormalize_address(self) -> None:
		"""Copy the target VM's public IPv6 onto `address`, so the desired-map query
		(port_map_for_region) is a single SELECT with no join. The proxy dials this
		literal; it never resolves a VM. A VM with no ipv6 yet is a hard error — an
		unaddressable target can't be a forwarding destination (mirrors Subdomain)."""
		address = frappe.db.get_value("Virtual Machine", self.virtual_machine, "ipv6_address")
		if not address:
			frappe.throw(
				f"Virtual Machine {self.virtual_machine} has no ipv6_address; cannot forward a port to it"
			)
		self.address = address


def port_pool() -> tuple[int, int]:
	"""The region's TCP port pool as an inclusive (low, high) range, read from
	Atlas Settings.tcp_port_pool (default 10000-19999). The pool must match the
	proxy's pre-opened `listen` range; growing it is a deliberate snapshot roll
	(spec/17-tcp-proxy.md)."""
	raw = frappe.db.get_single_value("Atlas Settings", PORT_POOL_FIELD) or DEFAULT_PORT_POOL
	try:
		low_str, high_str = str(raw).split("-", 1)
		low, high = int(low_str), int(high_str)
	except ValueError, AttributeError:
		frappe.throw(f"Atlas Settings.{PORT_POOL_FIELD} must be 'LOW-HIGH' (e.g. 10000-19999), got {raw!r}")
	if low > high:
		frappe.throw(f"Atlas Settings.{PORT_POOL_FIELD} range is inverted: {raw!r}")
	return low, high


def allocate_port(region: str) -> int:
	"""The lowest port in the region's pool not already held by ANY mapping in the
	region — active or inactive. An inactive row still owns its port (toggling it
	back on must not collide), so both count as taken. Pool exhaustion is a typed
	throw, never a silent wrap.

	Serialized under a row lock the same way the rest of Atlas allocates a scarce
	resource (allocate_ipv6 locks the Server): we SELECT the region's existing
	mappings FOR UPDATE so concurrent allocators in the same region queue behind
	each other. The `{region}-{public_port}` unique name is the final backstop for
	the first-row-in-an-empty-region race (one insert wins, the other retries)."""
	if not region:
		frappe.throw(_("Port Mapping needs a region before a port can be allocated"))
	low, high = port_pool()
	# FOR UPDATE on the region's rows serializes concurrent allocations; the lock
	# is released at transaction end. Reading the ports under the lock means a
	# second allocator sees this one's committed row before it picks.
	taken = {
		row["public_port"]
		for row in frappe.qb.from_("Port Mapping")
		.where(frappe.qb.Field("region") == region)
		.for_update()
		.select("public_port")
		.run(as_dict=True)
		if row["public_port"] is not None
	}
	for port in range(low, high + 1):
		if port not in taken:
			return port
	frappe.throw(
		f"TCP port pool exhausted for region {region}: all {high - low + 1} ports "
		f"({low}-{high}) are allocated. Grow Atlas Settings.{PORT_POOL_FIELD} "
		"(a deliberate proxy snapshot roll, spec/17-tcp-proxy.md)."
	)


def port_map_for_region(region: str) -> dict[str, str]:
	"""The desired port→backend map for a region: every ACTIVE mapping in the
	region, as `{"<public_port>": "[<address>]:<target_port>"}`. The value is a
	ready-to-dial bracketed-v6 host:port literal so the guest does no formatting.

	This is the full map every proxy VM in the region serves (spec/17-tcp-proxy.md
	"each proxy holds the whole regional map"). The TCP reconcile
	(atlas.atlas.tcp_proxy) compares this, serialized canonically, against each
	proxy guest's live map and bulk-`SYNC`s on drift."""
	rows = frappe.get_all(
		"Port Mapping",
		filters={"region": region, "active": 1},
		fields=["public_port", "address", "target_port"],
	)
	return {str(row["public_port"]): f"[{row['address']}]:{row['target_port']}" for row in rows}


def tcp_reconcile_region(region: str) -> None:
	"""Background-job entrypoint. Enqueued by Port Mapping's insert/active-toggle/
	delete hooks so a mapping change reaches the proxy fleet without the operator
	running a reconcile. Thin wrapper over atlas.atlas.tcp_proxy.reconcile_region —
	kept here (not a direct enqueue of tcp_proxy.reconcile_region) so the Port
	Mapping module owns its own background verb and the import stays lazy."""
	from atlas.atlas.tcp_proxy import reconcile_region

	reconcile_region(region)
