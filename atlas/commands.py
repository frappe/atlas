from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import click
import frappe
from frappe.commands import pass_context
from frappe.exceptions import SiteNotSpecifiedError
from frappe.utils.bench_helper import CliCtxObj

from atlas.atlas.core.host_binaries import (
	HostBinary,
	ensure_build_environment,
	find_host_binary,
	is_published,
	publish_host_binary,
	source_digest,
)
from atlas.atlas.core.setup import AtlasSetup, AtlasSetupConfiguration
from atlas.atlas.object_storage import ObjectStorageError
from atlas.metal_server.core.atlas_peer import AtlasPeer
from atlas.metal_server.core.development_gateway import DevelopmentGateway
from atlas.metal_server.doctype.metal_server.metal_server import MetalServer
from atlas.service.core.service_package import SERVICE_PACKAGES
from atlas.vm.core.image_builder import (
	ImagePurpose,
	build_ubuntu_image,
	build_ubuntu_rescue_image,
	get_available_ubuntu_images,
	publish_ubuntu_image,
)


@click.command("configure-atlas")
@pass_context
def configure_atlas(context: CliCtxObj) -> None:
	"""Configure each selected Atlas site from a JSON document on standard input."""
	if not context.sites:
		raise SiteNotSpecifiedError

	try:
		values: dict[str, Any] = json.load(click.get_text_stream("stdin"))
		configuration = AtlasSetupConfiguration.from_dict(values)
	except (json.JSONDecodeError, TypeError, ValueError) as error:
		raise click.UsageError(str(error)) from error

	for site in context.sites:
		try:
			frappe.init(site)
			frappe.connect()
			click.echo(f"Configure Atlas on {site}")
			AtlasSetup(configuration).run()
			click.echo(f"Atlas setup is complete on {site}")
		finally:
			frappe.destroy()


@click.command("build-metald")
@pass_context
def build_metald(context: CliCtxObj) -> None:
	"""Build metald and link it in Atlas Settings."""
	build_and_publish(context, find_host_binary("metald"))


@click.command("build-wg-mesh")
@pass_context
def build_wg_mesh(context: CliCtxObj) -> None:
	"""Build the Atlas WG Mesh CLI and link it in Atlas Settings."""
	build_and_publish(context, find_host_binary("wg-mesh"))


def build_and_publish(context: CliCtxObj, binary: HostBinary) -> None:
	"""Build one host binary for each site, unless its sources are unchanged."""
	if not context.sites:
		raise SiteNotSpecifiedError

	for site in context.sites:
		try:
			frappe.init(site)
			frappe.connect()

			digest = source_digest(binary)
			if is_published(binary, digest):
				click.echo(f"{binary.label} is current on {site}")
				continue

			ensure_build_environment((binary,))
			click.echo(f"Building {binary.label} for {site}")
			file_name = publish_host_binary(binary, digest)
			frappe.db.commit()  # nosemgrep
			click.echo(f"Published {binary.label} as File {file_name} on {site}")
		finally:
			frappe.destroy()


@click.command("build-service-packages")
@pass_context
def build_service_packages(context: CliCtxObj) -> None:
	"""Package each service component and link it in Atlas Settings."""
	if not context.sites:
		raise SiteNotSpecifiedError

	for site in context.sites:
		try:
			frappe.init(site)
			frappe.connect()

			for package in SERVICE_PACKAGES:
				archive = package.build_archive()
				digest = hashlib.sha256(archive).hexdigest()
				if package.is_published(digest):
					click.echo(f"{package.label} is current on {site}")
					continue

				file_name = package.publish_archive(archive, digest)
				frappe.db.commit()  # nosemgrep
				click.echo(f"Published {package.label} as File {file_name} on {site}")
		finally:
			frappe.destroy()


@click.command("build-ubuntu-base-image")
@click.option("--version", type=click.Choice(["22.04", "24.04"]), required=True)
@click.option("--architecture", type=click.Choice(["amd64"]), default="amd64", show_default=True)
@click.option("--minimal", is_flag=True, help="Build the Ubuntu minimal cloud image.")
@click.option("--title")
@click.option(
	"--skip-existing",
	is_flag=True,
	help="Do not build the image when a matching Available image exists.",
)
@click.option(
	"--storage",
	type=click.Choice(["object-storage", "site-file"]),
	default="object-storage",
	show_default=True,
	help="Store the artifacts in object storage, or as public site files during bootstrap.",
)
@click.option(
	"--output-directory", type=click.Path(path_type=Path), default=Path("./dist"), show_default=True
)
@pass_context
def build_ubuntu_base_image(
	context: CliCtxObj,
	version: str,
	architecture: str,
	minimal: bool,
	title: str | None,
	skip_existing: bool,
	storage: str,
	output_directory: Path,
) -> None:
	"""Build and publish a public Ubuntu server cloud image."""
	if not context.sites:
		raise SiteNotSpecifiedError
	if minimal and version != "24.04":
		raise click.UsageError("minimal images are available only for Ubuntu 24.04")

	title = title or f"ubuntu-{version}" + ("-minimal" if minimal else "")
	target_sites = context.sites
	if skip_existing:
		target_sites = [site for site in context.sites if not is_image_available(site, title, architecture)]
	if not target_sites:
		click.echo(f"Image {title} is already available")
		return

	click.echo(f"Building {title} for {architecture}")
	image_path, kernel_path = build_ubuntu_image(version, architecture, minimal, output_directory)
	_publish_image_to_sites(target_sites, title, version, architecture, image_path, kernel_path, storage)


@click.command("build-ubuntu-rescue-image")
@click.option("--title", default="ubuntu-24.04-rescue", show_default=True)
@click.option(
	"--skip-existing", is_flag=True, help="Skip sites with an Available rescue image of this title."
)
@click.option(
	"--storage",
	type=click.Choice(["object-storage", "site-file"]),
	default="object-storage",
	show_default=True,
)
@click.option(
	"--output-directory", type=click.Path(path_type=Path), default=Path("./dist"), show_default=True
)
@pass_context
def build_ubuntu_rescue_image_command(
	context: CliCtxObj, title: str, skip_existing: bool, storage: str, output_directory: Path
) -> None:
	"""Build and publish an independent Ubuntu 24.04 amd64 rescue image."""
	if not context.sites:
		raise SiteNotSpecifiedError
	target_sites = context.sites
	if skip_existing:
		target_sites = [
			site for site in context.sites if not is_image_available(site, title, "amd64", "rescue")
		]
	if not target_sites:
		click.echo(f"Image {title} is already available")
		return
	click.echo(f"Building {title} for amd64")
	image_path, kernel_path = build_ubuntu_rescue_image(output_directory)
	_publish_image_to_sites(target_sites, title, "24.04", "amd64", image_path, kernel_path, storage, "rescue")


def _publish_image_to_sites(
	target_sites: list[str],
	title: str,
	version: str,
	architecture: str,
	image_path: Path,
	kernel_path: Path,
	storage: str,
	purpose: ImagePurpose = "base",
) -> None:
	for site in target_sites:
		try:
			frappe.init(site)
			frappe.connect()
			click.echo(f"Publishing files to {site}")
			try:
				publish_ubuntu_image(
					title,
					version,
					architecture,
					image_path,
					kernel_path,
					"Site File" if storage == "site-file" else "Object Storage",
					purpose=purpose,
				)
			except ObjectStorageError as error:
				raise click.UsageError(str(error)) from error
			frappe.db.commit()  # nosemgrep
			click.echo(f"Created Virtual Machine Image for {site}")
		finally:
			frappe.destroy()


def is_image_available(site: str, title: str, architecture: str, purpose: ImagePurpose = "base") -> bool:
	"""Return true when a site has the requested system image."""
	try:
		frappe.init(site)
		frappe.connect()
		return bool(get_available_ubuntu_images(title, architecture, purpose))
	finally:
		frappe.destroy()


@click.command("configure-atlas-wireguard")
@pass_context
def configure_atlas_wireguard(context: CliCtxObj) -> None:
	"""Create the Atlas wg0 identity once and write its wg-quick file."""
	if not context.sites:
		raise SiteNotSpecifiedError

	for site in context.sites:
		try:
			frappe.init(site)
			frappe.connect()
			atlas_peer = AtlasPeer()
			atlas_peer.ensure_identity()
			atlas_peer.write_config()
			frappe.db.commit()  # nosemgrep
			click.echo(f"{site}: {atlas_peer.settings.wireguard_ip_address} {atlas_peer.config_path}")
		finally:
			frappe.destroy()


@click.command("deploy-dev-gateway")
@click.argument("metal_server")
@click.option(
	"--ssh-host",
	help="Reach a host that has no Atlas link yet, for example the first host by its public IPv4.",
)
@pass_context
def deploy_dev_gateway(context: CliCtxObj, metal_server: str, ssh_host: str | None = None) -> None:
	"""Run the development gateway on a Metal host and write the local wg-quick file."""
	if not context.sites:
		raise SiteNotSpecifiedError

	for site in context.sites:
		try:
			frappe.init(site)
			frappe.connect()
			config_path = DevelopmentGateway(frappe.get_doc("Metal Server", metal_server)).install(ssh_host)
			frappe.db.commit()  # nosemgrep
			click.echo(f"{site}: {config_path}")
		finally:
			frappe.destroy()


@click.command("import-metal-server")
@click.argument("provider_server_id")
@click.option("--storage-pool-device", help="Device or disk image file for the storage pool.")
@pass_context
def import_metal_server(context: CliCtxObj, provider_server_id: str, storage_pool_device: str | None) -> None:
	"""Add a provider server that was created outside Atlas, or continue its setup."""
	if not context.sites:
		raise SiteNotSpecifiedError

	for site in context.sites:
		try:
			frappe.init(site)
			frappe.connect()
			server = MetalServer.import_from_provider(provider_server_id, storage_pool_device)
			frappe.db.commit()  # nosemgrep
			click.echo(f"{site}: Metal Server {server.name} ({server.title}) is {server.status}")
		finally:
			frappe.destroy()


commands = [
	configure_atlas,
	build_metald,
	build_wg_mesh,
	build_service_packages,
	build_ubuntu_base_image,
	configure_atlas_wireguard,
	deploy_dev_gateway,
	import_metal_server,
]
