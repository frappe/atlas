from __future__ import annotations

from typing import TYPE_CHECKING, Any

import frappe

from atlas.api.core.base import (
	ApiResult,
	Page,
	add_tag_filter,
	build_page,
	get_owned_document,
)
from atlas.api.core.docs import api_docs
from atlas.api.core.errors import (
	ResourceConflict,
)
from atlas.api.models import (
	ImageDownloadQuery,
	ImageDownloadResponse,
	ImageListQuery,
	ImageResponse,
	TerminationProtectionPayload,
)
from atlas.api.router import images
from atlas.atlas.core.tags import read_tags_for
from atlas.auth.identity import get_current_tenant_id

if TYPE_CHECKING:
	from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage


def get_owned_image(image_id: str) -> VirtualMachineImage:
	"""Return one tenant image or a shared System image."""
	return get_owned_document("Virtual Machine Image", image_id)


@images.get("")
@api_docs()
def list_images(query: ImageListQuery) -> Page[ImageResponse]:
	"""List images.

	Returns one page of enabled System and Machine images owned by the tenant in newest-first order. A disabled image cannot boot a virtual machine, so the list leaves it out. Pass `image_type` as `system` or `machine` to return only that type. Omit it to return both.
	"""
	filters: dict[str, Any] = {"enabled": 1}
	if query.image_type:
		filters["image_type"] = query.image_type
	if not add_tag_filter("Virtual Machine Image", query, filters):
		return build_page([], query)

	rows: list[VirtualMachineImage] = frappe.get_list(
		"Virtual Machine Image",
		filters=filters,
		or_filters={"tenant_id": get_current_tenant_id(), "image_type": "system"},
		fields=[
			"name",
			"tenant_id",
			"title",
			"image_type",
			"architecture",
			"status",
			"enabled",
			"cache_image",
			"memory_snapshot",
			"is_termination_protected",
			"image_size_mib",
			"kernel.size_mib as kernel_size_mib",
			"transfer_progress",
			"transfer_error",
			"creation",
		],
		order_by="creation desc",
		offset=query.offset,
		limit=query.fetch_limit,
	)
	tags = read_tags_for("Virtual Machine Image", [row.name for row in rows])
	return build_page([ImageResponse.from_document(row, tags[row.name]) for row in rows], query)


@images.get("<image_id>")
@api_docs()
def get_image(image_id: str) -> ImageResponse:
	"""Get image.

	Returns one tenant image with its artifact metadata and transfer state.
	"""
	return ImageResponse.from_document(get_owned_image(image_id))


@images.get("<image_id>/download")
@api_docs(
	responses={200: {"description": "A signed artifact URL that expires after 24 hours."}},
)
def download_image(image_id: str, query: ImageDownloadQuery) -> ApiResult[ImageDownloadResponse]:
	"""Download image.

	Returns a signed download URL for the selected rootfs or kernel artifact. The response includes its size, SHA-256 value, and expiry time and cannot be cached.
	"""
	download = ImageDownloadResponse.from_download(
		get_owned_image(image_id).get_presigned_download_url(query.artifact)
	)
	return ApiResult(download, headers={"Cache-Control": "no-store"})


@images.delete("<image_id>")
@api_docs(
	responses={
		202: {"description": "The image is retired. Poll the image route while it is Deleting."},
		409: {"description": "Another tenant owns this System image."},
	},
)
def delete_image(image_id: str) -> ApiResult[ImageResponse]:
	"""Delete image.

	Retires an Available or Failed image that the tenant owns. A cleanup job removes the stored artifacts and remaining host snapshot data of an unused Machine image. Any other image becomes Archived and keeps its artifacts. An image that is already Deleting or Archived keeps that status and answers again.

	An image with `is_termination_protected` returns `400`. Clear the protection first.
	"""
	image = get_owned_image(image_id)
	if image.tenant_id != get_current_tenant_id():
		raise ResourceConflict("A shared System image of another tenant cannot be deleted.")
	image.request_deletion()
	return ApiResult(ImageResponse.from_document(image), status=202)


@images.patch("<image_id>/termination-protection")
@api_docs(
	request_example={"enabled": True},
	responses={202: {"description": "The termination protection state is stored."}},
)
def update_image_termination_protection(
	image_id: str, payload: TerminationProtectionPayload
) -> ApiResult[ImageResponse]:
	"""Update termination protection.

	Sets or clears termination protection. Deletion of a protected image is refused until a later request clears it. A `system` image is protected when it is created.
	"""
	image = get_owned_image(image_id)
	image.set_termination_protection(payload.enabled)
	return ApiResult(ImageResponse.from_document(image), status=202)
