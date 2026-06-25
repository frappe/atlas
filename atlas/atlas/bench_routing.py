"""Self-service subdomain routing for guest-created bench sites (spec/18).

A bench VM is a long-lived box whose owner spins up arbitrary sites from inside
the guest (the bench-admin UI or `bench new-site`). Atlas never ran `bench
new-site`, so no `Subdomain` row exists for those sites — yet they must become
routable through the regional proxy with no operator action, as long as the site
is named inside the regional wildcard (`<label>.<region>.frappe.dev`).

The design is **one-way push** (spec/18 "The shape"): the guest *tells* the
controller what changed; the controller never reads the guest back. There is **no
scheduled SSH pull and no sweeper** — no controller-initiated SSH into the guest at
all. Four whitelisted, guest-callable endpoints, each carrying **no VM-identifying
argument** (the controller resolves the calling VM from the request source address,
*Caller resolution*):

  register(label)     BEFORE `bench new-site` → the authoritative INSERT that
                                                RESERVES the name (active=1). The real
                                                block-at-create gate.
  deregister(label)   AFTER `bench drop-site`, OR rollback if new-site FAILS → DELETE
                                                the caller's own Subdomain (idempotent).
  check_label(label)  OPTIONAL pre-flight    → read-only advisory availability answer
                                                (UX nicety, NEVER the gate).
  list()              ON DEMAND               → read-only; the caller VM's own rows, to
                                                find + clear strays.

The controller stays the SINGLE authoritative writer of the fleet-wide-unique
`Subdomain` table (operating principle #2). `register`/`deregister` are **arbitrated,
not trusted**: the guest supplies a label and an intent, but every rule that protects
the fleet (uniqueness, reserved, brand denylist, per-VM cap, own-VM scoping) is applied
controller-side. The guest's word can create/remove only what the rules allow, only
for itself. `check_label`/`list` write nothing.

**Every call** — read or write, accepted or rejected — is recorded in the MyISAM
`Bench Routing Audit` log (Component I), audit-before-throw, so the rejected /
hijack-attempt rows survive the request rollback.

Teardown is one place: `VirtualMachine.terminate()` deletes every Subdomain for the
VM (Component F.1, in virtual_machine.py). There is no scheduled teardown here.
"""

import json

import frappe
from frappe.rate_limiter import rate_limit

from atlas.atlas.doctype.subdomain_denylist.subdomain_denylist import is_denylisted
from atlas.atlas.placement import active_root_domain, atlas_region
from atlas.atlas.subdomain_label import (
	RESERVED_SUBDOMAINS,
	is_taken,
	normalize,
	validate_label,
)

# The per-VM subdomain cap (Component G), keyed on the VM's memory_megabytes. A
# simple memory tier — 20 at the base, doubling a step at a time as the VM gets
# bigger — so a `resize()` re-prices it for free and adding a size is one row, never a
# recompute. The list is sorted by floor descending so the first match wins.
_CAP_TIERS = (
	(64 * 1024, 160),  # >= 64 GB
	(32 * 1024, 80),  # 32 GB
	(16 * 1024, 40),  # 16 GB
	(0, 20),  # the base — every size in sizes.py today (<= 8 GB) sits here
)


# ---------------------------------------------------------------------------
# Component A — register / deregister (the guest writes, the controller arbitrates)
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=60, seconds=60)
def register(label: str) -> dict:
	"""The authoritative insert, run **before** `bench new-site` (Component A).

	Reserves the name: this is the real block-at-create gate, not `check_label`.
	Resolves the calling VM from the request source `/128` (*Caller resolution*), runs
	the SAME Contract-A rules `Site`/`Site Request` enforce, in order — `validate_label`
	(shape) → reserved + brand denylist → fleet-wide availability → the per-VM cap —
	then inserts `Subdomain(subdomain=label, virtual_machine=<resolved vm>, region,
	active=1)` whose `after_insert` reconciles the proxy fleet (no extra push):

	    {"status": "ok" | "taken" | "reserved" | "at_limit" | "invalid"}

	The `ok` result also echoes `suffix` (the region domain) so the guest can build the
	FQDN it just reserved without a second round-trip — additive to the spec's minimal
	contract, the same region-domain `check_label` returns.

	A `DuplicateEntryError` (two benches racing the same label) maps to `taken` — the
	DB unique key is the atomic arbiter, and reserving FIRST is what makes the
	subsequent create un-blockable. `taken`/`reserved`/`at_limit`/`invalid` insert
	nothing and tell the guest why; the guest then never starts `bench new-site` (no
	orphan, no rollback). Idempotent on an already-owned label — a retry after a
	transient failure is a clean `ok`. Carries NO `vm_uuid` argument; the inserted row's
	`virtual_machine` is the source-resolved VM, never a param. Audited on every path."""
	label = normalize(label)
	vm = _resolve_caller_vm("register", label)  # throws (audited unresolved) on a bad source
	region, suffix = atlas_region(), active_root_domain().domain

	invalid = _label_invalid_reason(label)
	if invalid is not None:
		_audit("register", label, "invalid", business_reject=True, vm=vm.name)
		return {"status": "invalid", "reason": invalid}
	if _label_reserved(label):
		_audit("register", label, "reserved", business_reject=True, vm=vm.name)
		return {"status": "reserved"}

	# Idempotent on the caller's OWN row: a retried register for a label this VM already
	# owns is a clean ok (retry-after-transient). Checked before the fleet-availability
	# gate so an own-row retry never trips the "taken" branch against itself.
	if frappe.db.exists("Subdomain", {"subdomain": label, "virtual_machine": vm.name}):
		_audit("register", label, "ok", business_reject=False, vm=vm.name)
		return {"status": "ok", "suffix": suffix}

	if is_taken(label) or frappe.db.exists("Subdomain", {"subdomain": label}):
		_audit("register", label, "taken", business_reject=True, vm=vm.name)
		return {"status": "taken"}
	if _subdomain_count(vm.name) >= cap_for_vm(vm):
		_audit("register", label, "at_limit", business_reject=True, vm=vm.name)
		return {"status": "at_limit"}

	# The atomic arbiter: the DB unique key. Two benches racing the same free label
	# both pass the checks above; one wins the insert, the other's insert throws
	# DuplicateEntryError → taken. Reserving FIRST is what makes the create un-blockable.
	try:
		frappe.get_doc(
			{
				"doctype": "Subdomain",
				"subdomain": label,
				"region": region,
				"virtual_machine": vm.name,
				"active": 1,
			}
		).insert(ignore_permissions=True)
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		# The DB unique key is the atomic arbiter. `subdomain` is BOTH the autoname
		# source (the PRIMARY key) and `unique:1` (a secondary unique index), so a
		# losing race can surface as either: a PRIMARY-index dup → DuplicateEntryError,
		# a secondary-index dup → UniqueValidationError. Map both to `taken`.
		_audit("register", label, "taken", business_reject=True, vm=vm.name)
		return {"status": "taken"}
	_audit("register", label, "ok", business_reject=False, vm=vm.name)
	return {"status": "ok", "suffix": suffix}


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=60, seconds=60)
def deregister(label: str) -> dict:
	"""The teardown signal (Component A), fired on **two** paths: after a deliberate
	`bench drop-site`, AND as the rollback when `bench new-site` fails after a
	successful `register`. Resolves the calling VM (*Caller resolution*), finds its
	`Subdomain(subdomain=label, virtual_machine=<vm>)`, and deletes it — its `on_trash`
	deconverges the proxy:

	    {"status": "ok"}

	Scoped to the caller's OWN VM (a guest can never deregister another VM's route — the
	row's `virtual_machine` must match the resolved VM, else no-op). Idempotent: an
	absent row is a clean `ok` (a double drop, a replayed POST, a `list`-driven stray
	clear, or a rollback for a `register` that itself failed). Audited."""
	label = normalize(label)
	vm = _resolve_caller_vm("deregister", label)
	name = frappe.db.get_value("Subdomain", {"subdomain": label, "virtual_machine": vm.name})
	if name:
		frappe.delete_doc("Subdomain", name, ignore_permissions=True)
	_audit("deregister", label, "ok", business_reject=False, vm=vm.name)
	return {"status": "ok"}


# ---------------------------------------------------------------------------
# Component B — check_label (the optional advisory pre-flight)
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=60, seconds=60)
def check_label(label: str) -> dict:
	"""Read-only advisory availability answer (Component B), and **no longer the gate**
	— `register` is. An optional courtesy the guest may call to give the user early
	"that name's taken" feedback before committing to a `register`:

	    {"status": "ok" | "taken" | "reserved" | "at_limit" | "invalid",
	     "suffix": "<region domain>", "reason": "<message, invalid only>"}

	Runs the same checks `register` will (`validate_label`, reserved + denylist,
	`is_taken`, the per-VM cap against the source-resolved VM) and returns the active
	region's domain so the guest can build the FQDN without carrying it. Carries NO
	`vm_uuid` argument — the cap check is against the caller's own VM. Writes nothing
	but is audited.

	Advisory and fail-open by design — which is exactly *why* it can't be the gate: a
	wrong/stale "ok" acted on by starting a create and only then registering is the
	window an attacker could use to grab the name first; `register`'s atomic insert
	closes it. A malformed label is a clean `{"status": "invalid", "reason": …}` (not a
	417) so the guest hook surfaces the operator's message verbatim."""
	vm = _resolve_caller_vm("check_label", normalize(label))
	suffix = active_root_domain().domain
	label = normalize(label)

	invalid = _label_invalid_reason(label)
	if invalid is not None:
		_audit("check_label", label, "invalid", business_reject=True, vm=vm.name)
		return {"status": "invalid", "suffix": suffix, "reason": invalid}
	if _label_reserved(label):
		_audit("check_label", label, "reserved", business_reject=True, vm=vm.name)
		return {"status": "reserved", "suffix": suffix}
	# Don't count an own-row retry against the caller: a label this VM already owns
	# reads back as "taken" (it is — by this very VM), the honest advisory answer.
	if is_taken(label) or frappe.db.exists("Subdomain", {"subdomain": label}):
		_audit("check_label", label, "taken", business_reject=True, vm=vm.name)
		return {"status": "taken", "suffix": suffix}
	if _subdomain_count(vm.name) >= cap_for_vm(vm):
		_audit("check_label", label, "at_limit", business_reject=True, vm=vm.name)
		return {"status": "at_limit", "suffix": suffix}
	_audit("check_label", label, "ok", business_reject=False, vm=vm.name)
	return {"status": "ok", "suffix": suffix}


# ---------------------------------------------------------------------------
# Component C — list (the guest reads its OWN routes to find strays)
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=60, seconds=60)
def list() -> dict:
	"""Read-only enumeration of the caller VM's OWN routes (Component C). Takes **no
	argument** — the VM is the source address (*Caller resolution*), never a parameter:

	    {"domains": [{"label": "<label>",
	                  "fqdn":  "<label>.<region domain>",   # built controller-side
	                  "active": true | false}, ...]}        # [] for a VM with no rows

	Returns all `Subdomain` rows where `virtual_machine ==` the source-resolved VM.
	`fqdn` is reconstructed controller-side as `f"{label}.{region_domain}"` (never
	echoed from a guest suffix). Writes nothing and does NOT touch the cap (the cap
	counts on a write; enumerating consumes nothing). Audited.

	The guest's self-service stray finder: the owner compares these against its on-disk
	`sites/` and `deregister`s any routed label with no matching site (a lost
	`deregister`, the accepted-residual dead-link). A source matching no VM / a
	Terminated VM / a proxy is a clean reject (`frappe.throw`, no listing) — the same
	Caller-resolution gate the writes use; such a caller can't legitimately own bench
	sites. An empty inventory is the typed `{"domains": []}`, never a throw.

	(Shadows the `list` builtin at module scope deliberately — the wire method must be
	`atlas.atlas.bench_routing.list`. This module uses no `list(...)` builtin call.)"""
	vm = _resolve_caller_vm("list", "")
	region_domain = active_root_domain().domain
	rows = frappe.get_all(
		"Subdomain",
		filters={"virtual_machine": vm.name},
		fields=["subdomain", "active"],
	)
	domains = [
		{
			"label": row["subdomain"],
			"fqdn": f"{row['subdomain']}.{region_domain}",
			"active": bool(row["active"]),
		}
		for row in rows
	]
	_audit("list", "", "ok", business_reject=False, vm=vm.name)
	return {"domains": domains}


# ---------------------------------------------------------------------------
# Caller resolution (the VM is the source address, never a parameter)
# ---------------------------------------------------------------------------


def _resolve_caller_vm(endpoint: str, label: str):
	"""The VM whose packets reached the controller, resolved from the request's public
	IPv6 source `/128` (`frappe.local.request_ip`) matched against
	`Virtual Machine.ipv6_address` — NEVER from a request parameter (*Caller
	resolution*). A guest is root in its own VM and can read any injected value, so a
	guest-supplied `vm_uuid` could name another VM; the source address is the one
	VM-identifying fact the tenant cannot forge *if* it is read from a trusted edge that
	overwrites X-Forwarded-For (the hard prerequisite, spec/18 Caller resolution).

	A spoofed/non-matching source, a Terminated VM, or a proxy is a clean reject:
	`frappe.throw` after recording an `unresolved` audit row carrying the source `/128`
	that tried — a non-resolving source is exactly the forensic signal worth keeping.
	The reject `frappe.throw`s (rolling back the request transaction); the MyISAM audit
	row survives because the audit insert is non-transactional (Component I).

	`ipv6_address` is NOT a unique column, and `allocate_ipv6` can recycle a Terminated
	VM's `/128` onto a fresh Running VM (the reuse guard is deferred, spec/09). So we
	FILTER OUT Terminated and proxy rows IN THE QUERY — a stale Terminated row carrying
	the recycled address can never shadow the live owner of the `/128` — and FAIL CLOSED
	on ambiguity: if two *live* non-proxy VMs somehow share a `/128`, we resolve neither
	(a write under either would be wrong), rather than trusting an arbitrary first row."""
	source_ip = frappe.local.request_ip
	live = (
		frappe.get_all(
			"Virtual Machine",
			filters={"ipv6_address": source_ip, "status": ["!=", "Terminated"], "is_proxy": 0},
			pluck="name",
			limit=2,
		)
		if source_ip
		else []
	)
	if len(live) == 1:
		return frappe.get_doc("Virtual Machine", live[0])
	# No live non-proxy VM resolves (no source, no match, only Terminated/proxy rows, or
	# an ambiguous duplicate `/128`) → record the source `/128` with a blank vm, then
	# throw. The blank-vm + source_ip pair IS the spoof/ambiguity signal.
	_audit(endpoint, label, "unresolved", business_reject=True, vm="")
	frappe.throw(f"No bench VM resolves from the request source address {source_ip!r}")


# Region (Component E) is controller-resolved the same way `Site` does — from
# `Atlas Settings.region` (`placement.atlas_region`, the single source of truth),
# read inline in `register`.
# A site VM does NOT carry a `region` field (that's `depends_on: is_proxy`); we never
# make a VM-carried region a source of truth (it would drift and misroute) nor parse
# it from a guest FQDN. Single-region today; multi-region ties the VM to its region at
# provision and the resolver reads that — but resolution stays controller-side.


# ---------------------------------------------------------------------------
# Component G — the per-VM subdomain cap
# ---------------------------------------------------------------------------


def cap_for_vm(vm) -> int:
	"""The per-VM subdomain ceiling (Component G), a memory tier keyed on
	`memory_megabytes`: 20 at the base (every size in sizes.py today, <= 8 GB), doubling
	a step at a time (16 GB → 40, 32 GB → 80, >= 64 GB → 160). A `resize()` re-prices it
	for free; adding a size is one row in `_CAP_TIERS`, no recompute."""
	memory = int(vm.memory_megabytes or 0)
	for floor_mb, cap in _CAP_TIERS:
		if memory >= floor_mb:
			return cap
	return _CAP_TIERS[-1][1]


def _subdomain_count(vm_name: str) -> int:
	"""How many `Subdomain` rows this VM owns — the cap counts a *write*, so every row
	(active or not) consumes a slot. In this push-only model `register` always inserts
	`active=1` and `deregister` deletes, so a row is either present or gone."""
	return frappe.db.count("Subdomain", {"virtual_machine": vm_name})


# ---------------------------------------------------------------------------
# Component H — the brand/keyword denylist seam (shared by register + check_label)
# ---------------------------------------------------------------------------


def _label_invalid_reason(label: str) -> str | None:
	"""The operator-facing message if `label` fails the shape rules (`validate_label`),
	else None. Returned as a typed `invalid` result, not an HTTP error, so the guest
	hook surfaces it verbatim."""
	try:
		validate_label(label)
	except frappe.ValidationError as exception:
		return str(exception)
	return None


def _label_reserved(label: str) -> bool:
	"""True if `label` is blocked: the frozen structural set (`RESERVED_SUBDOMAINS`) OR
	the live brand denylist DocType (*Component H*). One seam both `register` and
	`check_label` call, so they reject the same labels in the same order — an operator's
	new denylist row is honored on the next call, no deploy, no migrate."""
	return normalize(label).lower() in RESERVED_SUBDOMAINS or is_denylisted(label)


# ---------------------------------------------------------------------------
# Component I — the request audit log (MyISAM, append-only, sole writer)
# ---------------------------------------------------------------------------


def _audit(endpoint: str, label: str, status: str, *, business_reject: bool, vm: str) -> None:
	"""Write one `Bench Routing Audit` row (Component I), the forensic backbone of the
	trust-root story. Called on EVERY path of EVERY endpoint, including the reject/throw
	paths (audit-before-throw).

	The row records both `source_ip` (the value caller resolution acted on,
	`frappe.local.request_ip`) and `fwd_headers` (the whole forwarded chain verbatim);
	when they disagree — a clean edge-supplied peer beside a guest-prepended
	`X-Forwarded-For: <other-/128>` — that divergence is the leftmost-XFF forgery signal.

	Persistence rides MyISAM's per-statement auto-commit ALONE — the helper does NOT
	call `frappe.db.commit()`. An explicit commit would flush any partial transactional
	work done before a later `frappe.throw`, defeating the rollback a reject relies on;
	the MyISAM engine's auto-commit (declared on the DocType) is the durability, so the
	reject's audit row survives the request rollback while its own InnoDB writes unwind.

	`vm` is a Data SNAPSHOT, not a Link — an audit row must outlive the VM's deletion,
	and a spoof attempt resolves to no VM (blank vm + the spoofing source_ip). Audit
	failure must never break the endpoint: the log is forensic, not load-bearing for the
	caller, so a write error is logged and swallowed."""
	try:
		frappe.get_doc(
			{
				"doctype": "Bench Routing Audit",
				"endpoint": endpoint,
				"label": label or "",
				"status": status,
				"business_reject": 1 if business_reject else 0,
				"vm": vm or "",
				"source_ip": frappe.local.request_ip or "",
				"fwd_headers": _forwarded_headers(),
				"request_body": _request_body(),
			}
		).insert(ignore_permissions=True)
	except Exception as exception:
		# The forensic log must not break the endpoint it observes.
		frappe.log_error(f"Bench routing audit insert failed: {exception}", "Bench routing audit")


def _forwarded_headers() -> str:
	"""The forwarded-header chain (incl. the raw X-Forwarded-For) verbatim — the
	guest-controlled bytes whose divergence from `source_ip` is the hijack signal.
	Empty outside a request context (a unit harness call), which is fine — the
	divergence test is a host fact, the unit boundary is "the row carries source_ip"."""
	if not getattr(frappe.local, "request", None):
		return ""
	wanted = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Forwarded")
	chain = {name: frappe.get_request_header(name) for name in wanted if frappe.get_request_header(name)}
	return json.dumps(chain) if chain else ""


def _request_body() -> str:
	"""The raw POST body verbatim (guest-controlled). Empty outside a request context."""
	request = getattr(frappe.local, "request", None)
	if request is None:
		return ""
	try:
		return request.get_data(as_text=True) or ""
	except Exception:
		return ""
