from __future__ import annotations

import hashlib
import json
import re
import shlex
from typing import TYPE_CHECKING, Any, NamedTuple

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime, time_diff_in_seconds
from frappe.utils.synchronization import LockTimeoutError, filelock

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.core.exceptions import AtlasConflictError, AtlasUserError
from atlas.atlas.core.tags import find_names_with_tags, read_tags_for
from atlas.atlas.object_storage import ObjectStorageError
from atlas.vm.core.models import VirtualMachineCreateRequest
from atlas.vm.core.multipart_upload import bytes_to_mib
from atlas.vm.core.placement.transaction import use_read_committed
from atlas.vm.core.vm_service import VirtualMachineService
from atlas.vm.doctype.virtual_machine.virtual_machine import PRIVILEGED_TENANT_ID
from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage

if TYPE_CHECKING:
	from atlas.atlas.object_storage import ObjectStorageClient

# Every builder VM runs under this tenant, so it reaches no tenant network.
BUILDER_TENANT_ID = 99999
BUILDER_CPU_MILLICORES = 2000
BUILDER_MEMORY_MIB = 2048
BUILD_TIMEOUT_SECONDS = 3600
# The tag on a builder VM that names the image it builds.
BUILDER_TAG = "oci_image"
IMPORT_LOCK_TIMEOUT_SECONDS = 10
MAXIMUM_BUILDS_PER_TENANT = 2
ARCHITECTURE = "amd64"

# The API calls these registries from the Atlas host, so a tenant cannot name an internal address.
DOCKER_HUB_REGISTRY = "registry-1.docker.io"
ALLOWED_REGISTRIES = frozenset(
	{DOCKER_HUB_REGISTRY, "ghcr.io", "quay.io", "public.ecr.aws", "mcr.microsoft.com"}
)
REFERENCE_PATTERN = re.compile(
	r"(?:(?P<registry>[^/]*[.:][^/]*)/)?(?P<repository>[a-z0-9._/-]+)"
	r"(?::(?P<tag>[\w.-]+))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?"
)
MANIFEST_MEDIA_TYPES = (
	"application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, "
	"application/vnd.docker.distribution.manifest.list.v2+json, "
	"application/vnd.docker.distribution.manifest.v2+json"
)
# A registry reports compressed sizes only. The disk holds the unpacked tree and the ext4 file at once.
BUILDER_DISK_FACTOR = 8
BUILDER_SPARE_DISK_MIB = 1024


class ResolvedImage(NamedTuple):
	"""One OCI image reference, pinned to the digest of its manifest."""

	reference: str
	digest: str
	compressed_bytes: int


class OCIImageImportService:
	"""Turn an OCI image into a boot image. A temporary VM builds and uploads the rootfs."""

	@use_read_committed
	def request(self, reference: str, tenant_id: int, title: str = "", image_type: str = "machine") -> str:
		"""Start one build, or return the image of this tenant that already has the same content."""
		if image_type == "system" and tenant_id != PRIVILEGED_TENANT_ID:
			frappe.throw(
				_("Only tenant {0} can create a System image.").format(PRIVILEGED_TENANT_ID),
				exc=AtlasUserError,
			)
		resolved = resolve_reference(reference)
		version = frappe.get_single("Atlas Settings").oci_rootfs_version
		# The lock spans the commit, so two requests cannot both pass the checks.
		try:
			with filelock(f"atlas:oci-image-import:{tenant_id}", timeout=IMPORT_LOCK_TIMEOUT_SECONDS):
				if existing := self.find_image(tenant_id, image_type, resolved.digest, version):
					return existing
				builds = frappe.db.count(
					"Virtual Machine Image", {"tenant_id": tenant_id, "status": "Building"}
				)
				if builds >= MAXIMUM_BUILDS_PER_TENANT:
					frappe.throw(
						_("Tenant {0} has {1} image builds running.").format(tenant_id, builds),
						exc=AtlasConflictError,
					)
				name = self.insert_image(resolved, tenant_id, title or reference, image_type, version)
				frappe.db.commit()  # nosemgrep
		except LockTimeoutError:
			frappe.throw(_("Another image import is in progress. Retry later."), exc=AtlasConflictError)

		self.enqueue(name)
		return name

	@staticmethod
	def find_image(tenant_id: int, image_type: str, digest: str, version: str) -> str | None:
		"""Return a usable image of this tenant from the same image digest and builder version."""
		names = find_names_with_tags(
			"Virtual Machine Image", {"oci_digest": digest, "oci_rootfs_version": version}
		)
		if not names:
			return None
		return frappe.db.get_value(
			"Virtual Machine Image",
			{
				"name": ["in", names],
				"tenant_id": tenant_id,
				"image_type": image_type,
				"status": ["in", ["Building", "Available"]],
			},
		)

	@staticmethod
	def insert_image(
		resolved: ResolvedImage, tenant_id: int, title: str, image_type: str, version: str
	) -> str:
		"""Insert the image in the Building status. Its compressed size sizes the builder disk."""
		name = frappe.generate_hash(length=16)
		frappe.get_doc(
			{
				"doctype": "Virtual Machine Image",
				"title": title,
				"image_type": image_type,
				"tenant_id": tenant_id,
				"architecture": ARCHITECTURE,
				"status": "Building",
				"image_object_key": f"images/{name}/rootfs.img",
				"image_stored_size_mib": bytes_to_mib(resolved.compressed_bytes),
				"kernel": VirtualMachineImage.get_latest_base_image(ARCHITECTURE).kernel,
				"tags": [
					{"key": "oci_reference", "value": resolved.reference},
					{"key": "oci_digest", "value": resolved.digest},
					{"key": "oci_rootfs_version", "value": version},
				],
			}
		).insert(set_name=name, ignore_permissions=True)
		return name

	@staticmethod
	def enqueue(image_name: str) -> None:
		"""Enqueue one repeatable step of a build."""
		frappe.enqueue(
			"atlas.vm.core.oci_image_import.advance_oci_image_build",
			queue="default",
			timeout=300,
			image_name=image_name,
			job_id=f"atlas||oci-image||{image_name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	def advance(self, image: VirtualMachineImage) -> None:
		"""Start the builder VM, or read its result. A transient failure repeats at the next poll."""
		try:
			client = frappe.get_single("Atlas Settings").get_object_storage_client()
			if time_diff_in_seconds(now_datetime(), image.creation) > BUILD_TIMEOUT_SECONDS:
				self.fail(image, _("The build did not finish in time."))
			# Draft reconciliation deletes a builder that Metal never received.
			elif not frappe.db.exists("Virtual Machine", image.source_virtual_machine or ""):
				image.db_set("source_virtual_machine", self.start_builder(image, client))
			elif (result := client.read_object(get_result_key(image.name))) is not None:
				self.finish(image, client, json.loads(result))
				# The result is the only proof of the build, so it stays until the image is saved.
				frappe.db.commit()  # nosemgrep
				client.delete_object(get_result_key(image.name))
		except (frappe.ValidationError, ObjectStorageError, ValueError) as error:
			frappe.db.rollback()
			image.db_set("transfer_error", str(error)[:1000])

	def start_builder(self, image: VirtualMachineImage, client: ObjectStorageClient) -> str:
		"""Create the builder VM and return its name. Create commits the VM with a tag that
		names the image, so a retry after a crash finds the VM instead of creating another."""
		if existing := find_names_with_tags("Virtual Machine", {BUILDER_TAG: image.name}):
			return existing[0]

		base_image = VirtualMachineImage.get_latest_base_image(ARCHITECTURE)
		result = VirtualMachineService.create(
			VirtualMachineCreateRequest(
				virtual_machine_image=base_image.name,
				cpu_millicores=BUILDER_CPU_MILLICORES,
				memory_mib=BUILDER_MEMORY_MIB,
				disk_mib=base_image.image_size_mib
				+ image.image_stored_size_mib * BUILDER_DISK_FACTOR
				+ BUILDER_SPARE_DISK_MIB,
				tenant_id=BUILDER_TENANT_ID,
				user_data=self.get_builder_script(image, client),
				tags={BUILDER_TAG: image.name},
			)
		)
		return result["name"]

	@staticmethod
	def get_builder_script(image: VirtualMachineImage, client: ObjectStorageClient) -> str:
		"""Return the boot script. The VM holds signed URLs only, never storage credentials."""
		upload_url = client.object_url(
			image.image_object_key, expiry_seconds=BUILD_TIMEOUT_SECONDS, method="PUT"
		)
		result_url = client.object_url(
			get_result_key(image.name), expiry_seconds=BUILD_TIMEOUT_SECONDS, method="PUT"
		)
		reference = next(tag.value for tag in image.tags if tag.key == "oci_reference")
		return "\n".join(
			(
				"#!/bin/sh",
				"set -e",
				f"curl -fsS --retry 5 -o /usr/local/bin/oci-rootfs {shlex.quote(get_builder_binary_url(client))}",
				"chmod +x /usr/local/bin/oci-rootfs",
				"exec oci-rootfs build -output /var/tmp/rootfs.ext4 "
				f"-upload-url {shlex.quote(upload_url)} -result-url {shlex.quote(result_url)} {shlex.quote(reference)}",
				"",
			)
		)

	def finish(self, image: VirtualMachineImage, client: ObjectStorageClient, result: dict[str, Any]) -> None:
		"""Store the artifact, or the error that the builder reported."""
		stored = client.head_object(image.image_object_key)
		if result.get("error") or not stored:
			self.fail(image, result.get("error") or _("The builder uploaded no root file system."))
			return
		image.update(
			{
				"status": "Available",
				"image_sha256": result.get("rootfs_sha256"),
				"image_size_mib": bytes_to_mib(int(result.get("rootfs_size_bytes", 0))),
				"image_stored_size_mib": bytes_to_mib(int(stored["ContentLength"])),
				"transfer_progress": 100,
				"transfer_error": None,
			}
		)
		image.save(ignore_permissions=True)

	@staticmethod
	def fail(image: VirtualMachineImage, message: str) -> None:
		image.status = "Failed"
		image.transfer_error = message[:1000]
		image.save(ignore_permissions=True)

	@staticmethod
	def remove_finished_builders() -> None:
		"""Terminate every builder VM whose image does not build. A failure repeats at the next poll."""
		names = frappe.get_all(
			"Virtual Machine",
			filters={"tenant_id": BUILDER_TENANT_ID, "is_terminating": 0, "is_draft": 0},
			pluck="name",
		)
		tags = read_tags_for("Virtual Machine", names)
		for name in names:
			image_name = tags[name].get(BUILDER_TAG)
			if (
				image_name
				and frappe.db.get_value("Virtual Machine Image", image_name, "status") == "Building"
			):
				continue
			try:
				VirtualMachineService(frappe.get_doc("Virtual Machine", name)).terminate()
			except frappe.ValidationError:
				frappe.log_error(title=f"Builder VM removal failed for {name}")


def get_result_key(image_name: str) -> str:
	return f"images/{image_name}/build-result.json"


def get_builder_binary_url(client: ObjectStorageClient) -> str:
	"""Return a signed URL of the published CLI. A builder VM cannot reach the Atlas site."""
	settings = frappe.get_single("Atlas Settings")
	key = f"builders/oci-rootfs-{settings.oci_rootfs_binary_hash}"
	if not client.head_object(key):
		file = frappe.get_doc("File", settings.oci_rootfs_binary_x86_64_file)
		client.upload_file(file.get_full_path(), key)
	return client.object_url(key, expiry_seconds=BUILD_TIMEOUT_SECONDS)


def resolve_reference(reference: str) -> ResolvedImage:
	"""Pin one OCI image reference to its digest and read its compressed size."""
	match = REFERENCE_PATTERN.fullmatch(reference)
	if not match:
		frappe.throw(_("Invalid image reference {0}.").format(reference), exc=AtlasUserError)
	registry = match["registry"] or DOCKER_HUB_REGISTRY
	if registry == "docker.io":
		registry = DOCKER_HUB_REGISTRY
	if registry not in ALLOWED_REGISTRIES:
		frappe.throw(_("Registry {0} is not allowed.").format(registry), exc=AtlasUserError)
	repository = match["repository"]
	if registry == DOCKER_HUB_REGISTRY and "/" not in repository:
		repository = f"library/{repository}"

	session = requests.Session()
	session.headers["Accept"] = MANIFEST_MEDIA_TYPES
	manifest_url = f"https://{registry}/v2/{repository}/manifests/"
	try:
		response = read_manifest(session, manifest_url + (match["digest"] or match["tag"] or "latest"))
		digest = hashlib.sha256(response.content).hexdigest()
		manifest = response.json()
		for candidate in manifest.get("manifests", []):
			platform = candidate.get("platform", {})
			if platform.get("os") == "linux" and platform.get("architecture") == ARCHITECTURE:
				manifest = read_manifest(session, manifest_url + candidate["digest"]).json()
				break
		compressed_bytes = sum(layer["size"] for layer in manifest.get("layers", []))
	except (requests.RequestException, KeyError) as error:
		frappe.throw(_("Cannot read image {0}: {1}").format(reference, error), exc=AtlasUserError)
	if not compressed_bytes:
		frappe.throw(_("Image {0} has no {1} layers.").format(reference, ARCHITECTURE), exc=AtlasUserError)
	return ResolvedImage(f"{registry}/{repository}@sha256:{digest}", digest, compressed_bytes)


def read_manifest(session: requests.Session, url: str) -> requests.Response:
	"""Read one manifest. A registry names its token service in a 401 answer."""
	response = session.get(url, timeout=30)
	if response.status_code == 401:
		fields = dict(re.findall(r'(realm|service|scope)="([^"]*)"', response.headers["WWW-Authenticate"]))
		token = session.get(fields.pop("realm"), params=fields, timeout=30)
		token.raise_for_status()
		session.headers["Authorization"] = f"Bearer {token.json()['token']}"
		response = session.get(url, timeout=30)
	response.raise_for_status()
	return response


def enqueue_oci_image_builds() -> None:
	"""Advance every running build and remove the builder VMs that finished."""
	service = OCIImageImportService()
	for name in frappe.get_all("Virtual Machine Image", filters={"status": "Building"}, pluck="name"):
		service.enqueue(name)
	service.remove_finished_builders()


@run_as_admin
def advance_oci_image_build(image_name: str) -> None:
	"""Advance one build."""
	image: VirtualMachineImage = frappe.get_doc("Virtual Machine Image", image_name)
	if image.status == "Building":
		OCIImageImportService().advance(image)
