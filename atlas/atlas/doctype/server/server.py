import hashlib
import json
import secrets
import shlex
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas import scripts_catalog
from atlas.atlas.providers.fake_tasks import is_fake_server
from atlas.atlas.ssh import connection_for_server, run_ssh, run_task, ssh_key_file, upload_files
from atlas.atlas.task_results import parse_result

# --- WHERE THE BOAT ARTIFACTS COME FROM ---------------------------------------
#
# Atlas routes ten host verbs and every VM lifecycle verb at `boat`
# (scripts_catalog.BOAT_VERBS, boat_client), so a host without boat is a host
# where those fail — one at a time, mid-flow. Bootstrap therefore installs it,
# and this is where the artifacts it installs come from.
#
# Atlas cannot build Go. It also must not assemble the install out of pieces:
# the binary, the sudoers allow-list and the two units are version-locked to each
# other in the boat repo — `go test ./internal/allowlist/` fails when the
# allow-list and the binary's own call sites disagree — so a host given the
# allow-list of one build and the binary of another has grants that do not match
# the commands it renders. Atlas ships ONE DIRECTORY the operator produced from
# ONE boat checkout, and never a file it composed itself:
#
#     git clone https://github.com/frappe/boat && cd boat && make build
#     # then in site_config.json:  "atlas_boat_distribution": "/path/to/boat"
#
# The relative paths below are that checkout's own layout after `make build`
# (bin/boat, sudoers.d/boat, systemd/*.service), which an unpacked release
# tarball shares — so "a checkout" and "a release" are the same instruction.
#
# WHY NOT A SIGNED RELEASE FETCHED HERE. Boat has signed-release verification and
# an atomic self-install (internal/update, spec/33-boat.md §5), and that is the
# right channel for the SECOND binary a host receives. It cannot be the first:
# the verification runs inside a daemon that is not installed yet, and
# `POST /v1/update` needs a boat to post it to. Landing the first binary over the
# SSH connection Atlas already holds is what spec/33 §4 specifies — "landing the
# binary reuses the existing SSH path" — and it is the only channel that exists
# before the host has boat on it.
#
# WHY SITE CONFIG AND NOT ATLAS SETTINGS. Every other Boat knob already lives
# there (`atlas_boat_base_urls`, `atlas_boat_tokens`, `atlas_boat_port`), and this
# one is a path on the CONTROLLER's filesystem: a property of the machine running
# bench, not fleet policy worth replicating into a DB row on every site.
#
# WHAT IS VERIFIED, since no signature is checked on this path: the four
# artifacts must exist locally (a missing one throws, naming the path and the
# command that produces it); the SHA-256 Atlas computed before the upload must
# equal the one the host reports after the swap, so a truncated or altered
# transfer never becomes the binary; and the landed binary must run — its
# `boat version` is what lands on `Server.observed_boat_version`, which until now
# was only ever written by the mirror sweep, long after the fact. When spec/33
# §5's desired `Server.boat_version` exists, comparing the two is the drift check
# and this is the place it goes.
DEFAULT_BOAT_DISTRIBUTION = "/opt/boat"

# Host paths. The binary is staged in its OWN directory (see _install_boat step 3
# for why the rename has to stay on one filesystem); the rest stage under
# /var/lib/atlas/boat, where they also serve as the record of what was installed.
BOAT_BINARY = "/usr/local/bin/boat"
BOAT_INCOMING_BINARY = f"{BOAT_BINARY}.incoming"
BOAT_STAGING_DIRECTORY = "/var/lib/atlas/boat"
BOAT_ARTIFACTS: list[tuple[str, str]] = [
	("bin/boat", BOAT_INCOMING_BINARY),
	("sudoers.d/boat", f"{BOAT_STAGING_DIRECTORY}/sudoers"),
	("systemd/boat.service", f"{BOAT_STAGING_DIRECTORY}/boat.service"),
	("systemd/boat-networkd.service", f"{BOAT_STAGING_DIRECTORY}/boat-networkd.service"),
]

# Where the daemon reads its bearer token, and the token's lifetimes (spec/33 §12).
# The daemon serves a minted token until BOAT_TOKEN_TTL_DAYS past minting and
# refuses it after (fail-closed); Atlas re-mints within BOAT_TOKEN_REMINT_WITHIN_DAYS
# of that on every bootstrap/upgrade, so a reachable host is always handed a fresh
# token well before the hard expiry — and a leaked-but-unrotated one is still bounded.
BOAT_TOKEN_PATH = "/etc/boat/token"
DATUM_TOKENS_PATH = "/etc/boat/datum-tokens.json"
DATUM_DROPIN_PATH = "/etc/systemd/system/boat.service.d/10-datum.conf"
BOAT_TOKEN_TTL_DAYS = 30
BOAT_TOKEN_REMINT_WITHIN_DAYS = 7


def boat_distribution() -> Path:
	"""The boat checkout (or unpacked release) on the CONTROLLER that bootstrap
	ships to hosts. `atlas_boat_distribution` in site config, else /opt/boat."""
	return Path(frappe.conf.get("atlas_boat_distribution") or DEFAULT_BOAT_DISTRIBUTION)


IMMUTABLE_AFTER_INSERT = (
	"title",
	"provider_type",
	"provider_resource_id",
	"size",
	"image",
	"ipv4_address",
	"ipv6_address",
	"ipv6_prefix",
	"ipv6_virtual_machine_range",
)


class Server(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		architecture: DF.Data | None
		boat_token: DF.Password | None
		boat_token_expires_at: DF.Datetime | None
		cli_ready: DF.Check
		firecracker_version: DF.Data | None
		image: DF.Link | None
		ipv4_address: DF.Data | None
		ipv6_address: DF.Data | None
		ipv6_prefix: DF.Data | None
		ipv6_virtual_machine_range: DF.Data | None
		jailer_version: DF.Data | None
		kernel_version: DF.Data | None
		mirror_error: DF.SmallText | None
		mirror_status: DF.Literal["", "Fresh", "Unknown"]
		observed_at: DF.Datetime | None
		observed_boat_version: DF.Data | None
		observed_quarantined: DF.SmallText | None
		observed_units_down: DF.SmallText | None
		provider_metadata: DF.Code | None
		provider_resource_id: DF.Data | None
		provider_type: DF.Literal["", "DigitalOcean", "Scaleway", "Self-Managed", "Fake"]
		size: DF.Link | None
		signing_private_key: DF.Password | None
		signing_public_key: DF.Data | None
		status: DF.Literal["Pending", "Bootstrapping", "Active", "Draining", "Broken", "Archived"]
		title: DF.Data
	# end: auto-generated types

	BOOTSTRAP_ALLOWED_STATUS: ClassVar[set[str]] = {"Pending", "Bootstrapping", "Active", "Broken"}
	# Durable uploads beyond the atlas package (which _bootstrap_uploads()
	# computes from disk). The per-VM firecracker-vm@ hooks are boat verbs now
	# (`/usr/local/bin/boat vm-* %i`), so nothing is shipped here for them; what
	# remains are the always-on atlas-wake-trap daemon and atlas-pool.service,
	# which import the durable package under /var/lib/atlas/bin (their sys.path
	# shim adds that dir). The package itself replaces the old durable lvm.sh —
	# there is no shell helper library anymore.
	BOOTSTRAP_UPLOAD_SOURCES: ClassVar[list[tuple[str, str]]] = [
		# The pip-install manifest: bootstrap-server.py runs `uv pip install
		# /var/lib/atlas/bin` into the Atlas venv, which needs a pyproject.toml at
		# that root. host-pyproject.toml's wheel package root is `atlas` (the flat
		# durable layout), distinct from the dev scripts/pyproject.toml.
		("host-pyproject.toml", "/var/lib/atlas/bin/pyproject.toml"),
		# install.sh creates the uv venv + `atlas` console script over SSH right
		# after this upload, BEFORE the bootstrap Task (which then runs as a normal
		# `atlas bootstrap-server` verb). Shipped durably so the controller has a
		# local copy to pipe over SSH — no public URL needed.
		("install.sh", "/var/lib/atlas/bin/install.sh"),
		# atlas-wake-trap.py is the always-on daemon that wakes a Sleeping VM on its
		# first inbound TCP SYN (spec/32). Shipped durably (it imports the durable
		# atlas package under /var/lib/atlas/bin); it is not a Task verb — the
		# scripts_catalog SYSTEMD_HOOKS set excludes it from the host run-task gate.
		("atlas-wake-trap.py", "/var/lib/atlas/bin/atlas-wake-trap.py"),
		("systemd/atlas-wake-trap.service", "/etc/systemd/system/atlas-wake-trap.service"),
		# The firecracker-vm@ unit's ExecStart* hooks are `/usr/local/bin/boat
		# vm-* %i` (vm-disk-up/vm-network-up/vm-restore/vm-network-down) — served by
		# the boat binary, so no per-VM hook file ships here anymore.
		("systemd/firecracker-vm@.service", "/etc/systemd/system/firecracker-vm@.service"),
		("systemd/atlas-pool.service", "/etc/systemd/system/atlas-pool.service"),
		# atlas-networkd.service (spec/31) is the long-running decentralized control
		# plane daemon that replaces host-mesh.service. It brings up wg-mesh + runs
		# gossip/anti-entropy/SWIM + programs wg-mesh atomically from the effective
		# Membership + Ownership tables. The keys/seed are written by bootstrap-
		# server.py under /etc/atlas-networkd/ before the service starts.
		("systemd/atlas-networkd.service", "/etc/systemd/system/atlas-networkd.service"),
	]

	def autoname(self) -> None:
		# UUID identity: title is the human label, name is opaque.
		self.name = str(uuid.uuid4())

	def validate(self) -> None:
		atlas_settings = frappe.get_single("Atlas Settings")
		atlas_settings._ensure_ancp_operator_keypair()
		atlas_settings._ensure_ancp_wg_derivation_secret()
		# ignore_mandatory: this save is incidental — we are only persisting the two
		# lazily-generated ANCP secrets, not asking the operator to have finished
		# configuring Atlas Settings. Without it, a Single with an empty `region`
		# (any site that has not been through setup(), including every fresh test
		# site) makes EVERY Server insert die with "Value missing for Atlas
		# Settings: Region" — an error naming a field the caller never touched.
		# The operator's required fields are still enforced when they save the
		# Single themselves.
		atlas_settings.flags.ignore_mandatory = True
		atlas_settings.save(ignore_permissions=True)
		self._validate_immutability()
		self._denormalize_mesh_identity()

	def _denormalize_mesh_identity(self) -> None:
		"""Fill the derived WireGuard host-mesh denorm fields (design §8). Both are pure
		functions of the Server UUID — the controller derives them so the seed carries
		the correct wg public key and the UI displays it legibly. The keypair is written
		to the host during bootstrap as `/etc/atlas-networkd/{wg-private-key,wg-public-key}`;
		the daemon reads those files in preference to self-generating.
		Set once; a re-derive yields the same value, so an existing row's fields are
		unchanged on save.

		Stage 5+ (spec/31 §19.4) — ALSO fill the ed25519 `signing_public_key` for
		this host. NOT derived (a derived signing key's seed would be public,
		defeating the purpose — §19.3). Generated ONCE at first validate.
		The matching private key is persisted as `signing_private_key` (encrypted
		Password) so the controller can write it to the host during bootstrap and
		push it to existing hosts when resyncing networkd state."""
		if not self.wireguard_public_key:
			from atlas.atlas.doctype.atlas_settings.atlas_settings import get_ancp_wg_derivation_secret
			from atlas.atlas.networking import derive_host_wireguard_keypair

			_private_key, self.wireguard_public_key = derive_host_wireguard_keypair(
				self.name, get_ancp_wg_derivation_secret()
			)
		if not self.mesh_address:
			from atlas.atlas.networking import derive_host_mesh_address

			self.mesh_address = derive_host_mesh_address(self.name)
		if not self.signing_public_key:
			from atlas.atlas.networking import generate_host_signing_keypair

			priv_b64, self.signing_public_key = generate_host_signing_keypair()
			# Persist the private key so it's available at bootstrap time and
			# for resync_networkd_keys. Silently skip if the field hasn't been
			# migrated yet (test env without bench migrate).
			try:
				self.signing_private_key = priv_b64
			except AttributeError:
				pass

	def _validate_immutability(self) -> None:
		"""Lock fields once they carry a value. Allow None → value transitions
		so the DigitalOcean provision flow (`finish_provisioning`) can write
		IPv4/6 onto a freshly-inserted Pending row whose addresses weren't
		known at insert time."""
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			old_value = getattr(original, field)
			new_value = getattr(self, field)
			if not old_value:
				continue  # initial population is allowed
			if old_value != new_value:
				frappe.throw(f"{field} is immutable after insert")

	@frappe.whitelist()
	def archive(self) -> None:
		"""Destroy the vendor resource (idempotent), then mark Archived.

		Resolve the vendor by the Server's OWN frozen `provider_type`, not the active
		one (`atlas.get_provider()`) — a host outlives a vendor switch, so destroy()
		must hit the client that owns the resource. Mirrors `reserved_ip.py`'s
		`_provider_for_server`."""
		from atlas.atlas.providers import for_provider_type

		if self.status == "Archived":
			frappe.throw(_("Server is already archived"))
		if self.provider_resource_id:
			for_provider_type(self.provider_type).destroy(self.provider_resource_id)
		frappe.db.set_value(self.doctype, self.name, "status", "Archived")

	@frappe.whitelist()
	def recover(self) -> bool:
		"""Operator escape hatch: re-drive a Server stranded pre-Active.

		`provision()` creates the billing vendor box synchronously, then a single
		fire-and-forget `finish_provisioning` job adopts it (describe → IPs →
		Bootstrapping → bootstrap → Active). When that job is lost the row sits in
		Pending / Bootstrapping forever with a paid-for box behind it. This re-enqueues
		finish_provisioning — the same path the scheduled reconciler uses, deduplicated
		so it never stacks a second job atop one still in flight.

		Distinct from `bootstrap()`: that runs the host bootstrap straight away and
		needs the IPs already populated, whereas a lost-job row has NULL addresses —
		recover() runs the full describe()-poll first to fill them. Returns True if a
		job was enqueued, False if one was already queued/running.
		"""
		from atlas.atlas.providers.worker import enqueue_finish_provisioning

		if self.status not in ("Pending", "Bootstrapping", "Broken"):
			frappe.throw(f"Cannot recover from status {self.status}; nothing is stuck")
		if not self.provider_resource_id:
			frappe.throw(
				"Server has no provider_resource_id — provision() never recorded a vendor "
				"resource, so there is nothing to recover. Re-provision instead."
			)
		return enqueue_finish_provisioning(self.name)

	@frappe.whitelist()
	def sync_image(self, image: str) -> str:
		"""Single-server convenience wrapper around `Virtual Machine Image.sync_to_server`."""
		image_doc = frappe.get_doc("Virtual Machine Image", image)
		return image_doc.sync_to_server(self.name)

	@frappe.whitelist()
	def bootstrap(self) -> str:
		"""Upload helpers, units and the boat artifacts, install boat, create the
		Atlas venv (install.sh), then run the host-prep Task. Returns Task name.

		Ordering is load-bearing, and each step is a precondition of the next:

		  1. the upload lands every artifact the steps below install from;
		  2. boat is installed, because install.sh's last gate is `command -v boat`
		     and the host-prep Task IS `boat bootstrap`;
		  3. install.sh's `uv pip install` needs the uploaded /var/lib/atlas/bin and
		     leaves the venv the remaining `atlas <verb>` Tasks run on;
		  4. `boat bootstrap` brings the host to VM-ready;
		  5. boat.service is started last — it adopts what the host holds, and there
		     is nothing to adopt until step 4 has laid the tree (see _start_boat).
		"""
		if self.status not in self.BOOTSTRAP_ALLOWED_STATUS:
			frappe.throw(f"Cannot bootstrap from status {self.status}")

		# A Fake server has no host to scp the durable package onto, no boat to
		# install and nothing to SSH install.sh into; the host-prep Task below is
		# faked too and still records the host versions, so the row ends up Active
		# exactly as a real bootstrap leaves it. Skip every host step in lockstep.
		connection = None if is_fake_server(self.name) else connection_for_server(self)
		if connection is not None:
			upload_files(connection, self._bootstrap_uploads())
			self._install_boat(connection)
			self._run_install_sh(connection)
			self._authorize_service_keys(connection)
			self._ship_dashboard(connection)
			self._write_ancp_bootstrap_state(connection)

		task = run_task(
			server=self.name,
			script="bootstrap",
			variables={
				"FIRECRACKER_VERSION": "v1.16.0",
				"ARCHITECTURE": "x86_64",
			},
		)
		self._absorb_bootstrap_output(task.stdout)
		if connection is not None:
			self._start_boat(connection)
		self.save(ignore_permissions=True)
		return task.name

	def _write_ancp_bootstrap_state(self, connection) -> None:
		"""Write the `/etc/atlas-networkd/` bootstrap files BEFORE the bootstrap-
		server Task starts `atlas-networkd.service`:
		- `wg-{private,public}-key` and `signing-{private,public}-key` — the host's
		  wg-mesh + ed25519 signing keypairs (spec/31 §7.1, §19.3, §19.4). The wg
		  half is derived from the Server UUID (`derive_host_wireguard_keypair`);
		  the ed25519 signing half is randomly generated ONCE at first
		  `Server.validate` (`generate_host_signing_keypair`) — never derived.
		- `identity.json` — this host's `(host_id, endpoint, mesh_address)`.
		- `seed.json` — every OTHER Active Server's `(host_id, endpoint,
		  wg_public_key, signing_public_key, mesh_address, generation=1)` (spec/31
		  §8, §19.4 — the seed now ALSO anchors each other host's ed25519 signing
		  pubkey so the envelope verifier's `signing_pubkey_cache` can be
		  pre-populated at build time).
		- `seed.json.sig` — the detached operator ed25519 signature over the exact
		  bytes of seed.json (spec/31 §9.2 / §19.4 — the seed is the sole trust
		  root; the host's `seed.load_seed` fails closed unless this verifies
		  against operator-public-key). Written only when the operator keypair is
		  configured (mirrors the operator-public-key / introduction-signature
		  gating).
		- TODO Stage 5+ (§19.5): `/etc/atlas-networkd/operator-public-key` — the
		  operator provision pubkey (the §19.5 newcomer trust root) and
		  `/etc/atlas-networkd/introduction-signature` — the operator-signed
		  `{host_id, signing_public_key, generation=1}` binding for THIS host
		  (present only when this host joins an existing cluster
		  post-bootstrap). Written below from the Atlas Settings operator
		  keypair when configured.
		After the first boot these files are stale (the daemon keeps its own
		state); they're only the initial seed-of-trust."""
		from atlas.atlas.networking import derive_host_mesh_address

		identity = {
			"host_id": self.name,
			"endpoint": self.ipv6_address,
			"mesh_address": self.mesh_address or derive_host_mesh_address(self.name),
		}
		# The seed = every OTHER Active Server (excluding this one). The daemon
		# will reconcile any drift via gossip+anti-entropy once it cold-joins.
		other_actives = frappe.get_all(
			"Server",
			filters={"status": "Active", "name": ["!=", self.name]},
			fields=["name", "ipv6_address", "wireguard_public_key", "mesh_address", "signing_public_key"],
		)
		seed = []
		for row in other_actives:
			if not row.ipv6_address:
				continue
			# §19.4 — the seed anchors each other host's ed25519 pubkey so the
			# envelope verifier's `signing_pubkey_cache` is populated at build
			# time. A legacy host bootstrapped before `signing_public_key`
			# existed has an EMPTY key here. SKIP it rather than emit an empty
			# entry: `seed.signing_pubkey_index` drops empty keys anyway, so an
			# emitted-empty entry gives the peer NO cached key AND forces the
			# §19.5 introduction path — but the introduction cert only rides the
			# legacy host's OWN first direct MembershipAdvertisement, never a
			# relayed/gossiped record, so a peer that first learns of it via a
			# relay silently drops (`signature_failed`) → one-sided partition.
			# Skipping is strictly safer: the peer just isn't seeded with this
			# host and learns it later once it HAS a key (the `backfill_server_
			# signing_key` migration fills every legacy row, so this is a
			# belt-and-braces guard for a row that slipped through). Warn loud so
			# an operator sees the gap instead of it silently partitioning.
			signing_public_key = getattr(row, "signing_public_key", "") or ""
			if not signing_public_key:
				frappe.logger("atlas").warning(
					f"skipping {row.name} from {self.name}'s ANCP seed: it has no "
					"signing_public_key (a host bootstrapped before the field existed). "
					"Run `bench migrate` (the backfill_server_signing_key patch) or "
					"resync_networkd_keys on it so it gets a signing key and can be seeded."
				)
				continue
			seed.append(
				{
					"host_id": row.name,
					"endpoint": row.ipv6_address,
					"wg_public_key": row.wireguard_public_key or "",
					"signing_public_key": signing_public_key,
					"mesh_address": row.mesh_address or derive_host_mesh_address(row.name),
					"generation": 1,
				}
			)
		from atlas.atlas.doctype.atlas_settings.atlas_settings import get_ancp_wg_derivation_secret
		from atlas.atlas.networking import derive_host_wireguard_keypair

		wg_private_key, _wg_public_key = derive_host_wireguard_keypair(
			self.name, get_ancp_wg_derivation_secret()
		)
		# Stage 5+ — the host's signing keypair. validate() generated one on first
		# insert and persisted the priv in `signing_private_key`. A re-Bootstrap
		# or resync reads it from the persisted field (encrypted Password) and
		# writes the key files again. If the field is empty (a host bootstrapped
		# before this migration), we read the existing keys from the host instead.
		#
		# IMPORTANT: `signing_private_key` is a Frappe Password field. Frappe's
		# `_save_passwords` (base_document.py) stores the plaintext encrypted in
		# `__Auth` and REPLACES the in-memory + column value with a `"****"` mask
		# of asterisks on every `save()`. `self.get()` returns the mask; reading
		# it back pushes `"****"` to `/etc/atlas-networkd/signing-private-key`,
		# `b64decode("****")` yields `b""`, `Ed25519PrivateKey.from_private_bytes
		# (b"")` raises, `keys._existing_signing_pair_valid` returns False → the
		# daemon silently regenerates a fresh keypair that doesn't match
		# `Server.signing_public_key` → every peer's envelope verifier drops the
		# host's MembershipAdvertisement → silent cluster partition. Use
		# `get_password` (which reads the decrypted plaintext from `__Auth`)
		# instead — the canonical Frappe way to read a Password field in code.
		pending_signing_priv = self.get_password("signing_private_key", raise_exception=False) or ""
		if pending_signing_priv:
			# Defensive in depth — refuse to push a non-ed25519-shaped priv.
			# `b64decode(validate=True)` rejects the `"****"` mask (which
			# contains non-base64 chars) and any other malformed value loud,
			# surfacing a regression here instead of letting the daemon mute-
			# regenerate a mismatched keypair.
			import base64

			try:
				priv_raw = base64.b64decode(pending_signing_priv, validate=True)
			except Exception as exc:
				frappe.throw(
					f"signing_private_key for {self.name} is not valid base64: {exc} — "
					"the field was likely read as the Frappe Password-field mask "
					"('****') instead of the decrypted plaintext"
				)
			if len(priv_raw) != 32:
				frappe.throw(
					f"signing_private_key for {self.name} is {len(priv_raw)} bytes, "
					"expected 32 (an ed25519 seed) — refusing to push a malformed "
					"signing key to the host (the daemon would silently regenerate "
					"a mismatched keypair and partition from the cluster)"
				)
		with ssh_key_file(connection.ssh_private_key) as key_path:
			run_ssh(
				connection,
				key_path,
				"sudo install -d -m 0755 {} && sudo install -m 0600 /dev/stdin {}",
				"/etc/atlas-networkd",
				"/etc/atlas-networkd/wg-private-key",
				timeout_seconds=30,
				stdin=wg_private_key + "\n",
			)
			run_ssh(
				connection,
				key_path,
				"sudo install -m 0644 /dev/stdin {}",
				"/etc/atlas-networkd/wg-public-key",
				timeout_seconds=30,
				stdin=_wg_public_key + "\n",
			)
			if pending_signing_priv and self.signing_public_key:
				# Stage 5+ — push the host's ed25519 signing keypair. The daemon's
				# `ensure_signing_keypair` is idempotent and validates the files;
				# if we wrote them here, the daemon reads them instead of generating.
				run_ssh(
					connection,
					key_path,
					"sudo install -m 0600 /dev/stdin {}",
					"/etc/atlas-networkd/signing-private-key",
					timeout_seconds=30,
					stdin=pending_signing_priv + "\n",
				)
				run_ssh(
					connection,
					key_path,
					"sudo install -m 0644 /dev/stdin {}",
					"/etc/atlas-networkd/signing-public-key",
					timeout_seconds=30,
					stdin=self.signing_public_key + "\n",
				)
				# CANARY — read back the on-disk signing-pub and assert it equals
				# `Server.signing_public_key`. If the daemon's `ensure_signing_keypair`
				# were about to regenerate (because the priv we pushed failed
				# validation), the on-disk pub would diverge from what the controller
				# signed the introduction cert over. Surface the divergence HERE,
				# at the controller, loud — the alternative is a silent cluster
				# partition on the next MembershipAdvertisement verify.
				read_back, _rb_err, rb_exit = run_ssh(
					connection,
					key_path,
					"sudo cat /etc/atlas-networkd/signing-public-key",
					timeout_seconds=30,
				)
				if rb_exit != 0 or (read_back or "").strip() != (self.signing_public_key or "").strip():
					frappe.throw(
						f"signing-public-key read-back from {self.name} "
						f"({(read_back or '').strip()!r}) doesn't match "
						f"Server.signing_public_key ({(self.signing_public_key or '').strip()!r}) — "
						"the daemon's ensure_signing_keypair is about to regenerate a "
						"mismatched keypair; the controller and host would diverge."
					)
		with ssh_key_file(connection.ssh_private_key) as key_path:
			run_ssh(
				connection,
				key_path,
				"sudo tee {} >/dev/null",
				"/etc/atlas-networkd/identity.json",
				timeout_seconds=30,
				stdin=json.dumps(identity, sort_keys=True) + "\n",
			)
			# The exact bytes we push to the host — sign THESE below so the
			# controller's signature is byte-identical to what the host's
			# `seed.load_seed` verifies (spec §9.2 / §19.4: the seed is the sole
			# trust root, so its operator signature is a hard load-time MUST).
			seed_content = json.dumps(seed, sort_keys=True) + "\n"
			run_ssh(
				connection,
				key_path,
				"sudo tee {} >/dev/null",
				"/etc/atlas-networkd/seed.json",
				timeout_seconds=30,
				stdin=seed_content,
			)
			# Stage 5+ (§19.5) — write the operator provision pubkey so the
			# host can verify any future newcomer's introduction certificate.
			# Also write the introduction-signature for THIS host when it's
			# joining an existing cluster (seed is non-empty → there are
			# existing hosts that don't know us yet) and the controller has
			# the operator priv key configured. Initial-seed hosts (seed is
			# empty → this is the first host in a fresh cluster) get no
			# introduction cert — every other host gets their pubkey via their
			# own seed.json on their own first boot. Empty operator pubkey
			# (no Atlas Settings keypair yet) means no §19.5 trust root; we
			# write nothing, leave the host's verifier fail-closed on any
			# future newcomer until the operator configures one.
			from atlas.atlas.doctype.atlas_settings.atlas_settings import (
				get_ancp_operator_private_key,
				get_ancp_operator_public_key,
			)

			operator_pub = get_ancp_operator_public_key()
			if operator_pub:
				run_ssh(
					connection,
					key_path,
					"sudo install -m 0644 /dev/stdin {}",
					"/etc/atlas-networkd/operator-public-key",
					timeout_seconds=30,
					stdin=operator_pub + "\n",
				)
				operator_priv = get_ancp_operator_private_key()
				# Re-use the host-lib's pure signing primitives (pure above the
				# keypair file — runs in the bench venv where `cryptography` is
				# already a dep). Use importlib to bypass the cached top-level
				# `atlas` package (the bench app) — sys.path insertion alone
				# won't reach scripts/lib/atlas/networkd/signing.py. Loaded once;
				# reused for the seed signature AND the introduction signature.
				if operator_priv:
					import importlib.util
					from pathlib import Path

					signing_path = str(
						Path(frappe.get_app_path("atlas")).parent
						/ "scripts"
						/ "lib"
						/ "atlas"
						/ "networkd"
						/ "signing.py"
					)
					_spec = importlib.util.spec_from_file_location("_host_signing", signing_path)
					_host_signing = importlib.util.module_from_spec(_spec)
					_spec.loader.exec_module(_host_signing)  # type: ignore[union-attr]

					# The seed is the sole trust root (spec §9.2 / §19.4), so the
					# host's `seed.load_seed` fails closed unless the exact bytes
					# of seed.json verify against operator_pub. Sign the SAME
					# bytes we pushed to /etc/atlas-networkd/seed.json above and
					# write the detached signature to the sibling seed.json.sig
					# (0644, matching the other pushed non-secret files).
					seed_sig = _host_signing.sign_detached(seed_content.encode("utf-8"), operator_priv)
					run_ssh(
						connection,
						key_path,
						"sudo install -m 0644 /dev/stdin {}",
						"/etc/atlas-networkd/seed.json.sig",
						timeout_seconds=30,
						stdin=seed_sig + "\n",
					)
					# A host joining an existing cluster (seed has peers → the
					# existing hosts didn't get us in their initial seed.json).
					# Sign {host_id, signing_public_key, generation=1} with the
					# operator priv; the §19.5 verifier accepts the self-asserted
					# signing_public_key iff this signature verifies against
					# operator_pub. Initial-seed hosts skip this (their pubkey is
					# already anchored on every peer via the seed).
					if seed and self.signing_public_key:
						intro_body = {
							"host_id": self.name,
							"signing_public_key": self.signing_public_key,
							"generation": 1,
						}
						intro_sig = _host_signing.sign_introduction(intro_body, operator_priv)
						run_ssh(
							connection,
							key_path,
							"sudo install -m 0600 /dev/stdin {}",
							"/etc/atlas-networkd/introduction-signature",
							timeout_seconds=30,
							stdin=intro_sig + "\n",
						)

	def _install_boat(self, connection) -> None:
		"""Install the boat binary, its allow-list, its service user and its units —
		the step that makes `boat <verb>` a command this host has. Runs after the
		upload (which staged all four artifacts) and before install.sh, whose last
		gate is `command -v boat`.

		THE ORDER IS THE POINT, and it is deliberately not the boat README's:

		  1. the `boat` system user. Both units run as it and every line of the
		     allow-list grants to it by name, so nothing below means anything until
		     it exists. Idempotent: a re-bootstrap finds it and adds nothing.
		  2. `visudo -cf` the STAGED allow-list, and only then `install` it 0440
		     root:root. Validate-then-install, never the README's reverse: a sudoers
		     file sudo cannot parse does not merely disable boat's grants, it takes
		     out the whole /etc/sudoers.d directory — the boat user ends up with no
		     grants at all and every verb on the host fails at once. Checking the
		     staged copy means an invalid file never reaches /etc.
		  3. the binary, renamed into place from a staging name in the SAME
		     directory. `mv` within one filesystem is rename(2), so a process that
		     execs /usr/local/bin/boat mid-install gets the whole old inode or the
		     whole new one and never a half-written file — and a running daemon
		     keeps its own open inode until it is restarted. That is
		     internal/update's Install reasoning, and the same reason it stages
		     inside /usr/local/bin rather than in /tmp.
		  4. the two units, then `daemon-reload`: systemd has to be told about a
		     unit file before anything asks it to start one.

		Starting boat.service is NOT here — see _start_boat for why it waits.
		"""
		digest = self._staged_boat_digest()
		steps = [
			(
				"creating the boat service user",
				"id boat >/dev/null 2>&1 || sudo useradd --system --home-dir /var/lib/boat "
				"--shell /usr/sbin/nologin boat",
			),
			("checking the sudoers allow-list", f"sudo visudo -cf {BOAT_STAGING_DIRECTORY}/sudoers"),
			(
				"installing the sudoers allow-list",
				f"sudo install -m 0440 -o root -g root {BOAT_STAGING_DIRECTORY}/sudoers /etc/sudoers.d/boat",
			),
			(
				"installing the boat binary",
				f"sudo chmod 0755 {BOAT_INCOMING_BINARY} && sudo mv -f {BOAT_INCOMING_BINARY} {BOAT_BINARY}",
			),
			(
				"installing boat.service",
				f"sudo install -m 0644 {BOAT_STAGING_DIRECTORY}/boat.service "
				"/etc/systemd/system/boat.service",
			),
			(
				"installing boat-networkd.service",
				f"sudo install -m 0644 {BOAT_STAGING_DIRECTORY}/boat-networkd.service "
				"/etc/systemd/system/boat-networkd.service",
			),
			("reloading systemd", "sudo systemctl daemon-reload"),
		]
		with ssh_key_file(connection.ssh_private_key) as key_path:
			for description, command in steps:
				self._boat_ssh(connection, key_path, description, command)
			self._verify_boat_binary(connection, key_path, digest)
			# The bearer token last: it is a per-host secret, minted here and written
			# to /etc/boat/token, not one of the four static artifacts the tar stream
			# carried. _start_boat's restart reads it.
			self._install_boat_token(connection, key_path)
			# The datum token bundle + its drop-in, when metrics export is configured. A
			# no-op otherwise, so a fleet without datum installs nothing extra.
			self._install_datum_tokens(connection, key_path)

	def _staged_boat_digest(self) -> str:
		"""SHA-256 of the boat binary this bootstrap is shipping, read on the
		controller. Compared against the host's after the swap: the tar stream fails
		loud on a broken transfer, but only the digest proves the bytes now at
		/usr/local/bin/boat are the bytes the operator staged."""
		return hashlib.sha256((boat_distribution() / "bin" / "boat").read_bytes()).hexdigest()

	def _verify_boat_binary(self, connection, key_path, digest: str) -> None:
		"""Prove the binary that landed is the one Atlas shipped, and that it runs.

		Two different failures, both silent without this: bytes that changed in
		flight (the digest), and a binary built for another architecture or a
		non-executable file (`boat version`, which is also the only way Atlas can
		learn the version — it cannot run a host's binary locally). The answer lands
		on `observed_boat_version` so the field records what was installed at
		install time rather than only what a later mirror sweep happened to see."""
		landed = self._boat_ssh(
			connection, key_path, "reading the installed digest", f"sha256sum {BOAT_BINARY}"
		)
		if landed.split()[:1] != [digest]:
			frappe.throw(
				f"boat on {self.name} is not the binary Atlas shipped: the host reports "
				f"{landed.split()[0] if landed.split() else '(nothing)'}, Atlas shipped {digest}"
			)
		version = self._boat_ssh(
			connection, key_path, "running boat version", f"{BOAT_BINARY} version"
		).strip()
		if not version:
			frappe.throw(f"`boat version` on {self.name} printed nothing; the binary is not usable")
		self.observed_boat_version = version

	def mint_boat_token(self) -> str:
		"""Mint this host's bearer token and stamp its hard expiry (spec/33 §12).

		Short-lived and per-host. The token is stored ENCRYPTED — a Password field, so
		it lives in __Auth and never in the row or a log — and returned so the caller
		installs the exact value without re-reading it. The expiry is the hard one the
		daemon enforces; the re-mint window (below) means a reachable host is handed a
		fresh token well before it and never reaches it.

		Written with set_encrypted_password + db_set rather than a full self.save():
		this runs mid-bootstrap/upgrade, and re-running every validate/before_save hook
		to stamp one credential is both needless and a way to trip a half-built doc."""
		from frappe.utils.password import set_encrypted_password

		token = secrets.token_urlsafe(32)
		set_encrypted_password(self.doctype, self.name, token, "boat_token")
		# Carry the token on the in-memory doc too: `Document._save_passwords`
		# REMOVES any Password field that is empty at save time, and bootstrap /
		# upgrade_boat both end with `self.save()` — so a mint that only wrote
		# __Auth was silently deleted by the very save that closed the operation,
		# leaving the host's /etc/boat/token the only copy (and the row tokenless
		# on the next read). Setting the attribute makes the trailing save re-write
		# the same value (then mask it), keeping row and host in agreement.
		self.boat_token = token
		self.db_set(
			"boat_token_expires_at",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), days=BOAT_TOKEN_TTL_DAYS),
		)
		return token

	def _current_or_minted_boat_token(self) -> str:
		"""The stored token if it is present and not yet within the re-mint window of
		its hard expiry, else a freshly minted one. Re-minting early is what keeps a
		reachable host from ever reaching the hard expiry the daemon fails closed on."""
		token = self.get_password("boat_token", raise_exception=False)
		expires = self.boat_token_expires_at
		if token and expires:
			remaining = frappe.utils.get_datetime(expires) - frappe.utils.now_datetime()
			if remaining.days > BOAT_TOKEN_REMINT_WITHIN_DAYS:
				return token
		return self.mint_boat_token()

	def _install_boat_token(self, connection, key_path) -> None:
		"""Write this host's bearer token to /etc/boat/token, minting or rotating it
		first. The file is the JSON form Boat reads — the token and the hard expiry it
		enforces — installed 0640 root:boat so the daemon user reads it and nobody
		else. The secret is piped on stdin, never argv (§12). _start_boat's restart
		reads it fresh; a rotation with no restart is this same write followed by
		`systemctl reload boat` (the daemon reloads the token on SIGHUP)."""
		token = self._current_or_minted_boat_token()
		payload = json.dumps(
			{
				"token": token,
				"hard_expires_at": frappe.utils.get_datetime(self.boat_token_expires_at).astimezone().isoformat(),
			}
		)
		self._boat_ssh(
			connection,
			key_path,
			"installing the boat token",
			f"sudo install -D -m 0640 -o root -g boat /dev/stdin {BOAT_TOKEN_PATH}",
			stdin=payload,
		)

	def _datum_dropin_contents(self) -> str:
		"""The systemd drop-in that turns metrics export on: it resets ExecStart and re-adds
		the daemon command with --server-name and the datum flags. NOTE: this fully owns
		ExecStart, so a host that also needs --listen must fold it in here too."""
		url = frappe.conf.get("atlas_datum_url")
		return (
			"[Service]\n"
			"ExecStart=\n"
			"ExecStart=/usr/local/bin/boat daemon --socket /run/boat/boat.sock "
			"--store /var/lib/boat/boat.db --token-file /etc/boat/token "
			f"--server-name {self.name} --datum-url {url} "
			"--datum-token-file /etc/boat/datum-tokens.json\n"
		)

	def _install_datum_tokens(self, connection, key_path) -> None:
		"""Ship this host's datum token bundle and the drop-in that points boat at datum.
		A no-op when metrics export is not configured (no atlas_datum_url), so a fleet that
		has not turned datum on installs nothing. The bundle is a single fleet token (resource_id="boat")
		with an empty VM map — host and VMs both report under it, told apart by server=/vm= labels; a
		static `atlas_datum_token` from site config is used when present, else a token is minted. The
		secret travels on stdin, never argv."""
		if not frappe.conf.get("atlas_datum_url"):
			return
		from atlas.atlas import datum_token

		payload = datum_token.single_token_file_json()
		self._boat_ssh(
			connection,
			key_path,
			"installing the datum tokens",
			f"sudo install -D -m 0640 -o root -g boat /dev/stdin {DATUM_TOKENS_PATH}",
			stdin=payload,
		)
		self._boat_ssh(
			connection,
			key_path,
			"installing the datum systemd drop-in",
			f"sudo install -D -m 0644 -o root -g root /dev/stdin {DATUM_DROPIN_PATH}",
			stdin=self._datum_dropin_contents(),
		)
		self._boat_ssh(connection, key_path, "reloading systemd for datum", "sudo systemctl daemon-reload")

	@frappe.whitelist()
	def refresh_datum_tokens(self) -> None:
		"""Re-mint and re-ship this host's datum bundle, then SIGHUP boat so it picks up the
		new tokens without a restart. This is the token-rotation path; bootstrap already
		installs the first bundle. A no-op on a Fake server or when datum is not configured."""
		if is_fake_server(self.name) or not frappe.conf.get("atlas_datum_url"):
			return
		connection = connection_for_server(self)
		with ssh_key_file(connection.ssh_private_key) as key_path:
			self._install_datum_tokens(connection, key_path)
			self._boat_ssh(
				connection, key_path, "reloading boat for datum rotation", "sudo systemctl reload boat.service"
			)

	def _start_boat(self, connection) -> None:
		"""Enable and (re)start boat.service — after the `boat bootstrap` Task.

		The order is not obvious, so: the daemon's first act is to scan the host and
		adopt what it finds (spec/33 §3.4), and on a fresh box there is nothing to
		find until `boat bootstrap` has made /var/lib/atlas, the thin pool and the
		nft scaffold. Starting it earlier points the adoption scan at a host that
		does not exist yet and makes a daemon fault indistinguishable from an
		unbootstrapped host.

		`restart`, not `start`: on a re-bootstrap the unit is already running the
		PREVIOUS binary and `start` would leave it there — the rename in
		_install_boat only takes effect on re-exec. `enable` is separate and
		idempotent, and is what survives a reboot.

		boat-networkd.service is installed but deliberately NOT started: ANCP is
		still served by atlas-networkd on these hosts, and two daemons programming
		one wg-mesh is worse than either.
		"""
		with ssh_key_file(connection.ssh_private_key) as key_path:
			self._boat_ssh(
				connection, key_path, "enabling boat.service", "sudo systemctl enable boat.service"
			)
			self._boat_ssh(
				connection, key_path, "starting boat.service", "sudo systemctl restart boat.service"
			)
			# is-active AFTER the restart: `systemctl restart` returns once the unit
			# has been exec'd (the unit is Type=exec), not once it has settled, so a
			# daemon that dies on its first read of the host exits 0 here.
			self._boat_ssh(
				connection, key_path, "boat.service did not stay up", "sudo systemctl is-active boat.service"
			)

	def _boat_ssh(
		self, connection, key_path, description: str, command: str, stdin: str | None = None
	) -> str:
		"""One step of the boat install, failing loud and naming which step. Every
		step is a precondition of the ones after it, so none may be logged past.

		`stdin`, when given, is piped to the remote command instead of placed in its
		argv — the token install uses it so the secret never appears in a process
		list, in the command string, or in this method's error text (§12; `install`
		reads /dev/stdin and echoes nothing, so stderr/stdout stay secret-free)."""
		stdout, stderr, exit_code = run_ssh(connection, key_path, command, timeout_seconds=120, stdin=stdin)
		if exit_code != 0:
			frappe.throw(
				f"boat install on {self.name}: {description} failed "
				f"(exit {exit_code}): {(stderr or stdout)[-500:]}"
			)
		return stdout

	def _run_install_sh(self, connection) -> None:
		"""Run scripts/install.sh on the host over SSH, AFTER the upload — it creates
		the uv venv + `atlas` console script and runs the deep sanity gate. This is
		what removes the bootstrap carve-out: once it returns, `bootstrap-server` runs
		as a normal `atlas <verb>` on the venv. Not recorded as a Task (it's bootstrap
		plumbing, like upload_files); raises on a non-zero exit so a broken venv fails
		the bootstrap HERE, before the bootstrap Task or any unit points at it."""
		command = "bash /var/lib/atlas/bin/install.sh"
		with ssh_key_file(connection.ssh_private_key) as key_path:
			stdout, stderr, exit_code = run_ssh(connection, key_path, command, timeout_seconds=600)
		if exit_code != 0:
			frappe.throw(
				f"install.sh failed on {self.name} (exit {exit_code}): {stderr[-500:] or stdout[-500:]}"
			)

	def _authorize_service_keys(self, connection) -> None:
		"""Append an external service's (e.g. chef) public key(s) to the host's root
		authorized_keys so the service can SSH the HOST for host-plane work (the
		mesh, the gateway — spec/30). Idempotent: a re-bootstrap never duplicates a line.
		No-op on an Atlas with no such service configured."""
		from atlas.atlas.atlas_settings import service_public_keys

		keys = service_public_keys()
		if not keys:
			return
		appends = " && ".join(
			f"grep -qxF {shlex.quote(key)} $AUTH || echo {shlex.quote(key)} >> $AUTH" for key in keys
		)
		command = (
			"AUTH=/root/.ssh/authorized_keys; mkdir -p /root/.ssh && chmod 700 /root/.ssh "
			f"&& touch $AUTH && chmod 600 $AUTH && {appends}"
		)
		with ssh_key_file(connection.ssh_private_key) as key_path:
			_stdout, stderr, exit_code = run_ssh(connection, key_path, command, timeout_seconds=60)
		if exit_code != 0:
			frappe.throw(
				f"authorizing Satellite keys on {self.name} failed (exit {exit_code}): {stderr[-300:]}"
			)

	def _ship_dashboard(self, connection) -> None:
		"""Build the read-only host dashboard on the controller and ship it to the
		host, then enable its socket unit. WHOLLY best-effort: the dashboard is a
		convenience, not part of the host's function, so nothing here may fail a
		bootstrap. A build that can't run (no npm/node_modules) ships nothing; an
		SSH error shipping or enabling it is logged and swallowed. Runs AFTER
		install.sh so a broken venv still surfaces as a hard bootstrap failure —
		the dashboard ships onto an already-good host or not at all.

		Freshness: dashboard.dashboard_uploads() ships assets ONLY from a build it
		just ran (dist/ is a gitignored artifact), so a re-bootstrap always lands
		current assets alongside a matching server.py, never a stale dist."""
		from atlas.atlas import dashboard

		try:
			uploads = dashboard.dashboard_uploads()
			if not uploads:
				return  # build could not be produced — skip silently, no unit enabled
			upload_files(connection, uploads)
			with ssh_key_file(connection.ssh_private_key) as key_path:
				_stdout, stderr, exit_code = run_ssh(
					connection, key_path, dashboard.enable_command(), timeout_seconds=60
				)
			if exit_code != 0:
				frappe.logger("atlas").warning(
					f"dashboard socket enable failed on {self.name} (exit {exit_code}): {stderr[-300:]}"
				)
		except Exception as exception:
			# Never let a dashboard hiccup fail a real bootstrap.
			frappe.logger("atlas").warning(f"dashboard ship skipped on {self.name}: {exception}")

	@frappe.whitelist()
	def sync_scripts(self) -> int:
		"""Re-upload the durable scripts (atlas package + systemd-invoked .py
		hooks) to /var/lib/atlas/bin without re-running bootstrap, then reinstall
		the atlas package into the venv so the new code is what imports resolve.

		The development fast path: after editing anything under scripts/lib/atlas/
		(or vm-network-up.py et al.) push the change to a live host in one scp
		sweep, instead of a full `bootstrap` (which also runs bootstrap-server.py
		and mutates status). Bootstrap remains the single refresh point for unit
		files; this is the subset that's pure code. Idempotent — a plain overwrite.

		The scp lands the package at /var/lib/atlas/bin/atlas, but every entry
		script and systemd hook imports `atlas` from the venv's site-packages,
		where install.sh COPY-installed it at bootstrap (`uv pip install`, not
		editable). Overwriting bin/atlas alone leaves that copy frozen — the edit
		never takes effect. So we `uv pip install --reinstall` the just-uploaded
		tree into the venv, exactly as install.sh's step 3 does; that is what makes
		sync a true code refresh rather than a dead-drop into bin/atlas.

		Returns the number of files uploaded.
		"""
		if not self.ipv4_address:
			frappe.throw(f"Server {self.name} has no ipv4_address; cannot sync scripts")
		connection = connection_for_server(self)
		uploads = self._script_uploads()
		upload_files(connection, uploads)
		self._reinstall_atlas_venv_package(connection)
		return len(uploads)

	def _reinstall_atlas_venv_package(self, connection) -> None:
		reinstall_atlas_venv_package(connection, self.name)

	@frappe.whitelist()
	def upgrade_boat(self) -> str:
		"""Bring an ALREADY-bootstrapped host up to the boat generation this
		controller ships — the binary, the sudoers allow-list and the two units —
		without a full `bootstrap` (which also re-runs `boat bootstrap`, mutates
		status, and re-prepares a host that is already VM-ready).

		This is the counterpart `sync_scripts` deliberately is not. `sync_scripts`
		refreshes only the pure /var/lib/atlas/bin code and leaves the binary and
		the units alone, because those need a privileged install and a daemon
		restart. But that left NO lighter path than a full re-`bootstrap` to deliver
		a new binary, a new allow-list line or an edited unit to a live host — so a
		fix to any of the three could reach a freshly bootstrapped host and no other
		(the deployment gap the split's audits kept naming: the binary, the
		allow-list and the units are three uncoupled hand-installs). This closes it:
		the boat-artifact + unit + durable-script subset of bootstrap, made a
		first-class, idempotent, re-runnable operation.

		Reuses bootstrap's own steps so the two can never drift:
		  1. upload the boat artifacts, the units and the durable scripts;
		  2. `_install_boat` — the service user, the validate-then-install sudoers,
		     the rename-into-place binary, the units, `daemon-reload`, and the
		     SHA-256 proof of what landed (which also records `observed_boat_version`);
		  3. `_start_boat` — `restart` so the running daemon re-execs the new inode
		     (the rename alone does not take effect until re-exec), then `is-active`
		     to prove it stayed up;
		  4. reinstall the durable atlas package into the venv so the refreshed hooks
		     are what imports resolve — exactly `sync_scripts`' last step.

		Idempotent: a host already at this generation re-ships identical bytes, the
		`mv -f`/`install` overwrite in place, and the digest check confirms the swap.
		Returns the `boat version` now installed. A Fake server has no host and is a
		no-op that records nothing, exactly as `bootstrap` skips its host steps."""
		if is_fake_server(self.name):
			return ""
		if not self.ipv4_address:
			frappe.throw(f"Server {self.name} has no ipv4_address; cannot upgrade boat")
		connection = connection_for_server(self)
		upload_files(connection, self._bootstrap_uploads())
		self._install_boat(connection)
		self._start_boat(connection)
		self._reinstall_atlas_venv_package(connection)
		self.save(ignore_permissions=True)
		return self.observed_boat_version or ""

	@frappe.whitelist()
	def reboot(self) -> str:
		"""Run reboot-server.sh as a Task. SSH drops mid-Task — Task ends in
		Failure; the operator confirms reboot by waiting and reconnecting."""
		return self.run_task_dialog(script="reboot-server", variables={})

	@frappe.whitelist()
	def run_task_dialog(self, script: str, variables: dict | str | None = None) -> str:
		"""Operator escape hatch. Same code path as bootstrap/provision.

		`variables` is a dict (JS form post) or JSON string. Returns Task name.
		"""
		if isinstance(variables, str):
			try:
				variables = json.loads(variables or "{}")
			except json.JSONDecodeError as exception:
				frappe.throw(f"variables must be valid JSON: {exception}")
		if variables is None:
			variables = {}
		if not isinstance(variables, dict):
			frappe.throw(_("variables must be a JSON object"))
		if script not in scripts_catalog.allowed_scripts():
			frappe.throw(f"Unknown script: {script}")
		task = run_task(
			server=self.name,
			script=script,
			variables=variables,
			timeout_seconds=1800,
		)
		return task.name

	@frappe.whitelist()
	def get_scripts(self) -> list[dict]:
		"""Whitelisted: operator-visible scripts + Run Task dialog metadata.

		Each entry is `{name, intro, fields}`. The client renders the dialog
		straight from this shape — fields are Frappe Dialog field dicts.

		The picker is intentionally shorter than `allowed_scripts()`.
		Lifecycle scripts (provision-vm, terminate-vm, vm-network-up, ...) are
		invoked from VM/Image controllers, not by hand from this dialog.
		"""
		return [
			{"name": name, **scripts_catalog.script_form(name)}
			for name in scripts_catalog.operator_visible_scripts()
		]

	def _bootstrap_uploads(self) -> list[tuple[str, str]]:
		return self._script_uploads() + self._unit_uploads() + self._boat_uploads()

	def _boat_uploads(self) -> list[tuple[str, str]]:
		"""The four boat artifacts, staged on the host for `_install_boat` to put in
		place. They ride the same one tar stream as everything else — a 15MB binary
		gzips to a few MB and a bootstrap is minutes long, so a second transport
		would buy nothing and cost a second thing to keep working.

		Deliberately NOT in `_script_uploads()`, which `sync_scripts()` also
		re-uploads: refreshing boat means a privileged install and a daemon restart,
		which is bootstrap's job. Shipping the binary on the dev fast path would
		leave a host whose /usr/local/bin/boat and running daemon disagree.

		Throws when the distribution is absent or incomplete, naming every missing
		path and the command that produces them: a host that fails here has nothing
		installed, where a host that fails later has half."""
		distribution = boat_distribution()
		uploads = [(str(distribution / source), destination) for source, destination in BOAT_ARTIFACTS]
		missing = [local for local, _destination in uploads if not Path(local).is_file()]
		if missing:
			frappe.throw(
				f"the boat distribution at {distribution} is missing {', '.join(missing)}. "
				"Build it (`git clone https://github.com/frappe/boat && cd boat && make build`) "
				"and point `atlas_boat_distribution` in site config at that directory — Atlas "
				"runs this host's verbs through boat and cannot build it."
			)
		return uploads

	def _script_uploads(self) -> list[tuple[str, str]]:
		"""The durable scripts that live under /var/lib/atlas/bin: the importable
		atlas package, the systemd-invoked .py hooks, and the Task entry scripts.
		These are pure code — an scp overwrite is all it takes for an edit to land,
		no daemon-reload. This is exactly the set `sync_scripts()` refreshes during
		development; bootstrap ships it alongside `_unit_uploads()`."""
		directory = scripts_catalog.scripts_directory()
		uploads = [
			(str(directory / source), destination)
			for source, destination in self.BOOTSTRAP_UPLOAD_SOURCES
			if destination.startswith("/var/lib/atlas/bin/")
		]
		# The durable atlas package: every lib module lands under
		# /var/lib/atlas/bin/atlas/ so the .py hooks and atlas-networkd can
		# `import atlas`. `rglob("*.py")` recurses into subdirectories so the
		# `atlas/networkd/` package ships alongside the flat modules — a flat
		# `glob("*.py")` missed subdirectory packages entirely. test_*.py files
		# are skipped (they're test-only, not shipped to hosts). __init__.py
		# files in subdirs are INCLUDED (they're what makes `atlas.networkd` an
		# importable package).
		package_dir = directory / "lib" / "atlas"
		for entry in sorted(package_dir.rglob("*.py")):
			if entry.name.startswith("test_"):
				continue
			rel = entry.relative_to(package_dir)
			uploads.append((str(entry), f"/var/lib/atlas/bin/atlas/{rel}"))
		# The durable Task entry scripts: every host SSH Task (provision-vm.py,
		# start/stop/snapshot-stop, …). `host_task_scripts()` yields VERBS; the FILE
		# (verb→file_for, e.g. provision-vm.py) is what ships — the file keeps its
		# suffix on the host disk, where `uv pip install` registers the console
		# entry and the runner reaches it as `atlas <verb>`. Shipping them here lets
		# the runner invoke each in place instead of scp'ing it per Task — the scp
		# was the dominant latency of an otherwise-instant start/stop. Computed from
		# disk (scripts_catalog) so a new Task script ships with no edit here.
		for verb in scripts_catalog.host_task_scripts():
			file_name = scripts_catalog.file_for(verb)
			uploads.append((str(directory / file_name), f"/var/lib/atlas/bin/{file_name}"))
		return uploads

	def _unit_uploads(self) -> list[tuple[str, str]]:
		"""The bootstrap-only uploads that are NOT plain /var/lib/atlas/bin code —
		systemd unit files under /etc/systemd/system. Editing one needs a
		daemon-reload (a bootstrap concern), so `sync_scripts()` deliberately omits
		these."""
		directory = scripts_catalog.scripts_directory()
		return [
			(str(directory / source), destination)
			for source, destination in self.BOOTSTRAP_UPLOAD_SOURCES
			if not destination.startswith("/var/lib/atlas/bin/")
		]

	def _absorb_bootstrap_output(self, stdout: str) -> None:
		# `boat bootstrap` emits the same typed BootstrapResult bootstrap-server.py
		# did, as one `ATLAS_RESULT=<json>` line; parse_result pulls it out (the host
		# still also writes /var/lib/atlas/bootstrap.json as the on-disk source of
		# truth). The keys are identical either way — that is what let the cutover be
		# the first word of the command and nothing else.
		#
		# The result also carries `python_version` (the resolved Atlas venv python).
		# It is deliberately NOT absorbed onto a Server field: it is derived state —
		# `/var/lib/atlas/venv/bin/python --version` on the host and the bootstrap
		# script's PY_VERSION constant are both live truth, so persisting a copy
		# would only drift. It rides the bootstrap log (this Task's stdout) for
		# visibility; nothing reads it back.
		parsed = parse_result(stdout)
		self.firecracker_version = parsed["firecracker_version"]
		self.jailer_version = parsed["jailer_version"]
		self.kernel_version = parsed["kernel_version"]
		self.architecture = parsed["architecture"]
		# The host's capacity totals ride the same BootstrapResult line (see
		# atlas.hostfacts). `.get()` because a Fake host's synthesized bootstrap
		# result omits them — its capacity comes from `fake_host_totals` in
		# `capacity_for_server`, so the row's totals stay unset and it reads as a
		# measured Fake host regardless. A real bootstrap always carries all three.
		self._stamp_capacity_facts(
			parsed.get("vcpus_total"),
			parsed.get("memory_megabytes_total"),
			parsed.get("pool_disk_gigabytes_total"),
		)
		# Reaching here means the bootstrap Task succeeded — and run_task raises on
		# any failure, so install.sh's deep sanity gate (which runs `atlas --help` to
		# prove the console script dispatches, and `boat version` to prove the other
		# host CLI is installed) passed earlier in the same bootstrap. Persist
		# CLI-readiness once, here, instead of paying a per-Task `test -e` round
		# trip: a legacy/unbootstrapped host has cli_ready=0 and the operator sees
		# the re-bootstrap signal. Fail-fast moved from per-Task to once-at-bootstrap.
		self.cli_ready = 1

	def _stamp_capacity_facts(
		self,
		vcpus_total: int | None,
		memory_megabytes_total: int | None,
		pool_disk_gigabytes_total: int | None,
		pool_data_percent: float | None = None,
	) -> None:
		"""Persist the host's measured capacity totals and the stamp time. Shared by
		bootstrap (three totals; pool fullness starts ~0, so it is left out) and
		Refresh Capacity (all four). `capacity_reported_at` records when the host was
		last measured, so a host silent past a staleness threshold can be treated as
		uncatalogued later rather than trusting stale totals (a future guard)."""
		self.vcpus_total = vcpus_total
		self.memory_megabytes_total = memory_megabytes_total
		self.pool_disk_gigabytes_total = pool_disk_gigabytes_total
		if pool_data_percent is not None:
			self.pool_data_percent = pool_data_percent
		self.capacity_reported_at = frappe.utils.now_datetime()

	@frappe.whitelist()
	def resync_networkd_keys(self) -> None:
		"""Re-push this host's ed25519 signing keypair and seed.json, then restart
		atlas-networkd. Fixes signing key mismatch between the controller and host
		(for hosts bootstrapped before signing_private_key was persisted).

		If signing_private_key is empty (migration case): read the existing signing
		keys from the host and adopt them as the canonical keys, so the controller
		matches what the host already has on disk. If the host has no signing keys
		either, generate a fresh keypair.

		After all hosts in the fleet are resynced, every host's seed.json carries
		the correct signing_public_key for every other host, and the daemon restarts
		with a correct `signing_pubkey_cache`.
		"""
		if self.status != "Active":
			frappe.throw(f"resync_networkd_keys requires Active status (got {self.status})")

		connection = connection_for_server(self)
		with ssh_key_file(connection.ssh_private_key) as key_path:
			# `signing_private_key` is a Frappe Password field — `self.get()`
			# returns the `"****"` mask after every `save()`, not the plaintext.
			# The mask reads as truthy, so a `not self.get(...)` guard would
			# always skip adoption and push the mask again (the same bug
			# `_write_ancp_bootstrap_state` had). Use `get_password` (reads the
			# decrypted plaintext from `__Auth`, returns `None` if the entry
			# doesn't exist) so adoption actually fires when there's no key.
			if not self.get_password("signing_private_key", raise_exception=False):
				_maybe_adopt_host_keys(self, connection, key_path)

			self._write_ancp_bootstrap_state(connection)
			_run_restart_networkd(connection, key_path)

	@frappe.whitelist()
	def refresh_capacity_facts(self) -> str:
		"""Re-measure the host's capacity facts and stamp them — the Refresh Capacity
		button. For an already-Active host whose shape changed (a resized droplet, a
		grown pool) or that was bootstrapped before the totals were reported. Runs the
		read-only `server-facts` Task and persists the four numbers; returns the Task
		name. Bootstrap already stamps the three totals, so this is the no-re-bootstrap
		refresh — and the one path that also captures live `pool_data_percent`."""
		if self.status != "Active":
			frappe.throw(f"Refresh capacity on an Active host (status is {self.status})")
		task = run_task(server=self.name, script="server-facts", variables={}, timeout_seconds=120)
		parsed = parse_result(task.stdout)
		self._stamp_capacity_facts(
			parsed["vcpus_total"],
			parsed["memory_megabytes_total"],
			parsed["pool_disk_gigabytes_total"],
			parsed["pool_data_percent"],
		)
		self.save(ignore_permissions=True)
		return task.name


def reinstall_atlas_venv_package(connection, server_name: str) -> None:
	"""Reinstall the durable /var/lib/atlas/bin tree into the Atlas venv so the
	just-synced code is what `import atlas` resolves to. Mirrors install.sh's
	step 3 (`uv pip install --reinstall`) verbatim — the venv holds a COPY, not an
	editable link, so a plain scp overwrite of bin/atlas would not reach it. The
	uv/venv literals match install.sh (UV_DIR / ATLAS_VENV / BIN_DIRECTORY); the
	two trees don't share imports, so the paths are repeated here. Pure SSH — safe
	to call from a sync_scripts_to_all worker thread."""
	command = (
		"sudo env VIRTUAL_ENV=/var/lib/atlas/venv "
		"/var/lib/atlas/uv/uv pip install --reinstall /var/lib/atlas/bin"
	)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		stdout, stderr, exit_code = run_ssh(connection, key_path, command, timeout_seconds=300)
	if exit_code != 0:
		frappe.throw(
			f"atlas venv reinstall failed on {server_name} (exit {exit_code}): "
			f"{stderr[-500:] or stdout[-500:]}"
		)


def _maybe_adopt_host_keys(server, connection, key_path: str) -> None:
	"""Read the host's existing signing keys and adopt them into the Server doc.
	The daemon generates its own signing keypair on first boot if the files don't
	exist yet. For a host bootstrapped before `signing_private_key` was persisted,
	the keys on disk are the canonical ones — we read them and save to the doc so
	the controller matches what the host already has (instead of forcing a new
	keypair that would break existing cache entries on peers)."""
	_stdout, _stderr, exit_code = run_ssh(
		connection,
		key_path,
		"sudo cat /etc/atlas-networkd/signing-private-key 2>/dev/null",
		timeout_seconds=30,
	)
	if exit_code != 0 or not _stdout.strip():
		from atlas.atlas.networking import generate_host_signing_keypair

		priv_b64, server.signing_public_key = generate_host_signing_keypair()
		server.signing_private_key = priv_b64
	else:
		host_priv = _stdout.strip()
		_stdout2, _stderr2, _exit2 = run_ssh(
			connection,
			key_path,
			"sudo cat /etc/atlas-networkd/signing-public-key 2>/dev/null",
			timeout_seconds=30,
		)
		if _exit2 == 0 and _stdout2.strip():
			host_pub = _stdout2.strip()
		else:
			from atlas.atlas.networking import generate_host_signing_keypair

			priv_b64, host_pub = generate_host_signing_keypair()
			host_priv = priv_b64
		server.signing_private_key = host_priv
		server.signing_public_key = host_pub
	server.save(ignore_permissions=True)


def _run_restart_networkd(connection, key_path: str) -> None:
	"""Restart atlas-networkd on the host so it re-reads the signing key files
	and seed.json. Best-effort: a restart failure is logged but not fatal — the
	host will pick up the new config on its next natural restart."""
	run_ssh(
		connection,
		key_path,
		"sudo systemctl restart atlas-networkd 2>/dev/null || true",
		timeout_seconds=30,
	)


def sync_scripts_to_all() -> dict[str, int]:
	"""Push the durable scripts to every Active server in one sweep.

	The development convenience: edit a script under scripts/lib/atlas/ once, then
	`bench --site <site> execute atlas.sync_scripts_to_all` (or `atlas.sync_scripts_to_all()`
	in a console) to refresh every live host. Active-only because a Pending/Broken
	server has no working SSH endpoint. Returns {server_name: files_uploaded}.

	Hosts are synced CONCURRENTLY: each host's cost is now dominated by its cold SSH
	handshake (a few seconds to a remote region), and those handshakes are
	independent I/O — a serial sweep pays them back-to-back (N x handshake), a
	parallel one overlaps them (~1 x handshake).

	All Frappe/DB work (the doc load, the connection, the upload list) is resolved
	HERE on the main thread first; the pool threads only do the pure-SSH push. That
	push still reaches Frappe for cosmetics (`frappe.utils.nowtime()` in the upload
	log line reads `frappe.local`, which is thread-local and empty in a fresh
	worker), so each worker binds its own Frappe context to the SAME site for the
	duration of its upload via `frappe_thread_context`."""
	names = frappe.get_all("Server", filters={"status": "Active"}, pluck="name")

	# Resolve everything that touches the DB on the main thread: the doc, its SSH
	# connection, and the file list. The thread only does the SSH upload.
	jobs = []
	for name in names:
		server = frappe.get_doc("Server", name)
		if not server.ipv4_address:
			frappe.logger("atlas").warning(f"sync-scripts skipping {name}: no ipv4_address")
			continue
		jobs.append((name, connection_for_server(server), server._script_uploads()))

	if not jobs:
		return {}

	site = frappe.local.site

	def _push(job) -> tuple[str, int]:
		name, connection, uploads = job
		try:
			with frappe_thread_context(site):
				print(f"Syncing durable scripts to {name} ({connection.host})")
				upload_files(connection, uploads)
				reinstall_atlas_venv_package(connection, name)
				print(f"Done syncing durable scripts to {name} ({connection.host})")
			return name, len(uploads)
		except Exception as exc:
			frappe.logger("atlas").warning(f"sync-scripts failed for {name} ({connection.host}): {exc}")
			return name, 0

	from concurrent.futures import ThreadPoolExecutor

	with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
		return dict(pool.map(_push, jobs))


def upgrade_all_hosts_to_current_boat(*, enqueue: bool = True) -> dict[str, str]:
	"""Bring every Active host to the boat generation this controller ships.

	The fleet-wide counterpart to `Server.upgrade_boat`, and the mechanism the
	`upgrade_hosts_to_current_boat` patch runs once after a boat change lands: a
	new binary, allow-list line or unit that `sync_scripts` cannot deliver reaches
	the whole fleet here. Active-only — a Pending/Broken host has no working SSH.

	`enqueue=True` (the default, and what the patch uses) queues ONE background job
	per host, so a `bench migrate` is never blocked on N privileged installs and
	daemon restarts over SSH and one slow or unreachable host cannot stall the rest
	or the migrate itself. Returns {server_name: "queued"}. `enqueue=False` runs
	them inline, serially and tolerantly — one host's failure is logged and does not
	stop the next — returning {server_name: installed_version | "error: …"}, the
	shape an operator watches from a `bench execute`/console sweep."""
	names = frappe.get_all("Server", filters={"status": "Active"}, pluck="name")
	results: dict[str, str] = {}
	for name in names:
		if enqueue:
			frappe.enqueue(
				"atlas.atlas.doctype.server.server.upgrade_host_to_current_boat",
				queue="long",
				timeout=1800,
				job_id=f"upgrade-boat-{name}",
				deduplicate=True,
				server_name=name,
			)
			results[name] = "queued"
			continue
		try:
			results[name] = frappe.get_doc("Server", name).upgrade_boat() or "ok"
		except Exception as exc:
			frappe.logger("atlas").warning(f"upgrade-boat failed for {name}: {exc}")
			results[name] = f"error: {exc}"
	return results


def upgrade_host_to_current_boat(server_name: str) -> None:
	"""Background-job entry for `upgrade_all_hosts_to_current_boat(enqueue=True)`:
	upgrade one host and commit. The doc is loaded fresh inside the job so nothing
	stale crosses the enqueue boundary, and a failure surfaces in the job record
	rather than a caller's stack."""
	frappe.get_doc("Server", server_name).upgrade_boat()
	frappe.db.commit()


@contextmanager
def frappe_thread_context(site: str):
	"""Bind a Frappe context to `site` for the current thread, then tear it down.

	`frappe.local` is thread-local, so a worker thread spawned off the request/CLI
	main thread starts with no site bound — any `frappe.*` that reads `local` (e.g.
	`frappe.utils.nowtime()` reaching for the site timezone) raises `AttributeError:
	conf`. Init + connect gives the worker its own bound context and DB connection
	(NOT shared with the main thread's, which would be unsafe); `destroy()` closes
	it so the thread leaves nothing behind. Read-mostly here — the upload does no
	writes — but each worker owning its connection keeps it correct if that changes."""
	frappe.init(site=site)
	frappe.connect()
	try:
		yield
	finally:
		frappe.destroy()


def refresh_datum_tokens_for_server(server_name: str) -> None:
	"""Enqueue target for datum_token.refresh_all: load the Server and re-ship its bundle."""
	frappe.get_doc("Server", server_name).refresh_datum_tokens()
