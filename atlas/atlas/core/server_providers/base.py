from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic, sleep
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

import frappe

PollResult = TypeVar("PollResult")

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.server.doctype.server.server import Server


@dataclass(frozen=True, slots=True)
class SizeInfo:
	"""Vendor server size data."""

	size: str
	cpu_count: int
	memory_mb: int
	disk_gib: int
	hourly_pricing_usd_cents: int | None
	monthly_pricing_usd_cents: int | None
	provider_metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ImageInfo:
	"""Vendor server OS image data."""

	image: str
	os: str
	version: str
	provider_metadata: Mapping[str, Any] | None = None


class ServerProviderError(Exception):
	"""Raised when a provider cannot complete a server operation."""


ACCEPTED_OS_VERSIONS: Mapping[str, tuple[str, ...]] = MappingProxyType(
	{
		"Ubuntu": ("22.04", "24.04", "26.04"),
		"Debian": ("11", "12", "13"),
	}
)


class ServerProvider(ABC):
	"""Base class for providers registered with Atlas Settings."""

	provider_type: ClassVar[str]
	credential_fields: ClassVar[tuple[str, ...]]
	server_setup_fields: ClassVar[tuple[str, ...]] = (
		"status",
		"is_provisioning_completed",
		"provider_server_id",
		"provider_metadata",
		"public_ipv4_address",
		"private_ipv4_address",
		"public_network_interface",
		"private_network_interface",
	)
	ssh_users: ClassVar[tuple[str, ...]] = ("root", "ubuntu")
	ssh_timeout_seconds: ClassVar[int] = 1_000
	ssh_poll_interval_seconds: ClassVar[int] = 1

	def __init__(self, settings: "AtlasSettings | None" = None) -> None:
		self.settings: AtlasSettings = settings or frappe.get_single("Atlas Settings")

	@abstractmethod
	def bootstrap(self) -> None:
		"""Set up the provider resources required by Atlas."""
		...

	@abstractmethod
	def validate_settings(self) -> None:
		"""Check the provider fields when Atlas Settings validates."""
		...

	@abstractmethod
	def validate_credentials(self) -> bool:
		"""Use before provider operations to check the configured credentials."""
		...

	@abstractmethod
	def create_private_network(self, cidr: str) -> str:
		"""Return the existing private network ID, or create one."""
		...

	@abstractmethod
	def create_ssh_key(self, public_key: str) -> str:
		"""Return the existing SSH key ID, or upload the Atlas public key."""
		...

	@abstractmethod
	def fetch_server_sizes(self) -> tuple[SizeInfo, ...]:
		"""Return the server sizes available from the vendor."""
		...

	@abstractmethod
	def fetch_server_images(self) -> tuple[ImageInfo, ...]:
		"""Return the server images available from the vendor."""
		...

	@property
	def provisioning_steps(self) -> tuple[Callable[["Server"], None], ...]:
		"""Return the post-creation setup functions in execution order."""
		return ()

	@abstractmethod
	def create_server(self, server: "Server") -> None:
		"""Create the remote server before Atlas inserts the Server document."""
		...

	@abstractmethod
	def reboot_server(self, server: "Server") -> None:
		"""Reboot the provider server."""
		...

	@abstractmethod
	def poweroff_server(self, server: "Server") -> None:
		"""Stop the provider server."""
		...

	@abstractmethod
	def poweron_server(self, server: "Server") -> None:
		"""Start the provider server."""
		...

	@abstractmethod
	def archive_server(self, server: "Server") -> None:
		"""Delete the provider server that backs an Atlas Server.

		This must succeed when the provider server is already gone.
		"""
		...

	def cleanup_provisioned_server(self, server: "Server") -> None:
		"""Remove a newly created provider server after a local provisioning failure."""
		return None

	def run_provisioning(self, server: "Server") -> None:
		"""Run the post-creation setup functions in order."""
		try:
			server.status = "Installing"
			self.save_server_setup_progress(server)
			for provisioning_step in self.provisioning_steps:
				provisioning_step(server)
				self.save_server_setup_progress(server)
			server.status = "Running"
			server.is_provisioning_completed = 1
			self.save_server_setup_progress(server)
		except Exception:
			server.status = "Failed"
			self.save_server_setup_progress(server)
			raise

	def wait_for_ssh(self, server: "Server") -> None:
		"""Wait for root SSH access on a provider server."""
		from atlas.atlas.core.ssh import wait_for_server

		if not server.public_ipv4_address:
			raise ServerProviderError("Server has no public IPv4 address")

		try:
			user = wait_for_server(
				host=server.public_ipv4_address,
				users=self.ssh_users,
				timeout_seconds=self.ssh_timeout_seconds,
				poll_interval_seconds=self.ssh_poll_interval_seconds,
			)
		except TimeoutError as error:
			raise ServerProviderError(str(error)) from error
		if user == "root":
			return

		self.promote_ssh_user(server, user)
		try:
			root_user = wait_for_server(
				host=server.public_ipv4_address,
				users=("root",),
				timeout_seconds=self.ssh_timeout_seconds,
				poll_interval_seconds=self.ssh_poll_interval_seconds,
			)
		except TimeoutError as error:
			raise ServerProviderError("Root SSH did not become ready after user promotion") from error
		if root_user != "root":
			raise ServerProviderError("Root SSH did not become ready after user promotion")

	def promote_ssh_user(self, server: "Server", user: str) -> None:
		"""Promote a provider SSH user to root access."""
		raise ServerProviderError(f"Provider cannot promote SSH user {user}")

	def run_setup_script(
		self,
		server: "Server",
		script: str,
		*,
		ssh_user: str = "root",
		environment: Mapping[str, object] | None = None,
		timeout_seconds: int = 120,
	) -> None:
		"""Run a packaged setup script and require a successful SSH result."""
		from atlas.server.doctype.server_ssh_task.server_ssh_task import ServerSSHTask

		task = ServerSSHTask.create_for_script_file(
			server=server.name,
			script_path=script,
			ssh_user=ssh_user,
			environment=environment,
			timeout_seconds=timeout_seconds,
			run_in_background=False,
		)
		result = task.result
		if result is None or not result.is_success:
			output = result.output.strip() if result else "The SSH task did not complete"
			raise ServerProviderError(f"Setup script {script} failed: {output}")

	def poll(
		self,
		operation: Callable[[], PollResult | None],
		*,
		timeout_seconds: int,
		poll_interval_seconds: int,
		description: str,
		on_retry: Callable[[], None] | None = None,
	) -> PollResult:
		"""Poll an operation until it returns a result or times out."""
		deadline = monotonic() + timeout_seconds
		while monotonic() < deadline:
			result = operation()
			if result is not None:
				return result
			if on_retry:
				on_retry()
			sleep(poll_interval_seconds)
		raise ServerProviderError(f"Timed out while waiting for {description}")

	@classmethod
	def save_server_setup_progress(cls, server: "Server") -> None:
		"""Write the provider-owned fields and commit before the next provider call.

		Server setup holds one Server document for several minutes. A full save()
		compares timestamps and fails when anything else writes the same Server, so
		this writes only the fields the provider owns.
		"""
		server.db_set({field: server.get(field) for field in cls.server_setup_fields})
		frappe.db.commit()

	def sync_provider_sizes(self) -> None:
		"""Sync provider sizes with Server Size records."""
		for size in self.fetch_server_sizes():
			name = f"{self.provider_type}/{size.size}"
			metadata_json = frappe.as_json(size.provider_metadata)
			if frappe.db.exists("Server Size", name):
				document = frappe.get_doc("Server Size", name)
				if (
					document.cpu_count == size.cpu_count
					and document.memory_mb == size.memory_mb
					and document.disk_gib == size.disk_gib
					and document.hourly_pricing_usd_cents == size.hourly_pricing_usd_cents
					and document.monthly_pricing_usd_cents == size.monthly_pricing_usd_cents
					and document.provider_metadata == metadata_json
				):
					continue

				document.cpu_count = size.cpu_count
				document.memory_mb = size.memory_mb
				document.disk_gib = size.disk_gib
				document.hourly_pricing_usd_cents = size.hourly_pricing_usd_cents
				document.monthly_pricing_usd_cents = size.monthly_pricing_usd_cents
				document.provider_metadata = metadata_json
				document.save(ignore_permissions=True)
				continue

			frappe.get_doc(
				{
					"doctype": "Server Size",
					"provider_type": self.provider_type,
					"size": size.size,
					"cpu_count": size.cpu_count,
					"memory_mb": size.memory_mb,
					"disk_gib": size.disk_gib,
					"hourly_pricing_usd_cents": size.hourly_pricing_usd_cents,
					"monthly_pricing_usd_cents": size.monthly_pricing_usd_cents,
					"provider_metadata": metadata_json,
				}
			).insert(ignore_permissions=True)

	def sync_provider_images(self) -> None:
		"""Sync provider images with Server Image records."""
		for image in self.fetch_server_images():
			name = f"{self.provider_type}/{image.image}"
			metadata_json = frappe.as_json(image.provider_metadata)
			if frappe.db.exists("Server Image", name):
				document = frappe.get_doc("Server Image", name)
				if document.provider_metadata == metadata_json:
					continue

				document.provider_metadata = metadata_json
				document.save(ignore_permissions=True)
				continue

			frappe.get_doc(
				{
					"doctype": "Server Image",
					"provider_type": self.provider_type,
					"image": image.image,
					"provider_metadata": metadata_json,
				}
			).insert(ignore_permissions=True)
