"""Unit tests for `ScalewayProvider` and the `ScalewayClient` mapping helpers.

Mocks `ScalewayClient` exactly as `test_digitalocean.py` mocks the DO client —
the provider class is the seam between business logic and the HTTP wrapper, so
mocking the client keeps the tests fast and offline. Catalog-row lookups
(`offer_id` / `os_id`) are patched at `_metadata_value` so the tests don't need
seeded Provider Size / Provider Image rows.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.providers.base import (
	Networking,
	ProviderError,
	ProvisionRequest,
	SshKey,
)


def _build_provider():
	"""Build a ScalewayProvider with stubbed Settings and client."""
	from atlas.atlas.providers import scaleway as scw_module

	settings = SimpleNamespace(
		zone="fr-par-1",
		project_id="proj-uuid",
		organization_id="org-uuid",
		billing="hourly",
		default_size="Scaleway/EM-A610R-NVMe",
		default_image="Scaleway/Ubuntu_24.04",
	)
	with (
		patch.object(frappe, "get_single", return_value=settings),
		patch.object(scw_module, "get_secret", return_value="scw-secret-key"),
		patch.object(scw_module, "ScalewayClient") as client_cls,
	):
		provider = scw_module.ScalewayProvider()
		provider.client = MagicMock()
		client_cls.return_value = provider.client
	return provider


class TestScalewayProviderAuthenticate(IntegrationTestCase):
	def test_authenticate_ok(self) -> None:
		provider = _build_provider()
		provider.client.verify_credentials.return_value = {
			"account_label": "my-project",
			"project_count": 2,
		}
		result = provider.authenticate()
		self.assertTrue(result.ok)
		self.assertEqual(result.account_label, "my-project")
		provider.client.verify_credentials.assert_called_once_with("org-uuid")

	def test_authenticate_bad_returns_error_without_raising(self) -> None:
		from atlas.atlas.scaleway import ScalewayError

		provider = _build_provider()
		provider.client.verify_credentials.side_effect = ScalewayError("GET /account/v3/projects -> 401")
		result = provider.authenticate()
		self.assertFalse(result.ok)
		self.assertIn("401", result.error)


class TestScalewayProviderDiscover(IntegrationTestCase):
	def test_discover_maps_offers_and_os_and_filters_by_billing(self) -> None:
		provider = _build_provider()
		provider.client.list_offers.return_value = [
			{
				"id": "offer-uuid-1",
				"name": "EM-A610R-NVMe",
				"subscription_period": "hourly",
				"price_per_month": {"currency_code": "EUR", "units": 39, "nanos": 990000000},
			}
		]
		provider.client.list_os.return_value = [{"id": "os-uuid-1", "name": "Ubuntu", "version": "24.04"}]
		caps = provider.discover()
		# list_offers gets the billing mode so hourly/monthly offers don't mix.
		provider.client.list_offers.assert_called_once_with(subscription_period="hourly")
		self.assertEqual(len(caps.sizes), 1)
		size = caps.sizes[0]
		self.assertEqual(size.slug, "EM-A610R-NVMe")
		# 39.99 EUR rounds to 40; the offer_id is stashed for provision().
		self.assertEqual(size.monthly_cost_usd, 40)
		self.assertEqual(size.provider_metadata["offer_id"], "offer-uuid-1")
		self.assertEqual(len(caps.images), 1)
		image = caps.images[0]
		self.assertEqual(image.slug, "Ubuntu_24.04")
		self.assertEqual(image.provider_metadata["os_id"], "os-uuid-1")

	def test_discover_strips_marketing_name_from_image_slug(self) -> None:
		"""Scaleway's OS version carries the marketing name ("24.04 LTS (Noble
		Numbat)"); the slug keeps only the leading version token so the
		operator-facing handle stays terse. The raw version stays in metadata."""
		provider = _build_provider()
		provider.client.list_offers.return_value = []
		provider.client.list_os.return_value = [
			{"id": "os-uuid-2", "name": "Ubuntu", "version": "24.04 LTS (Noble Numbat)"}
		]
		caps = provider.discover()
		self.assertEqual(caps.images[0].slug, "Ubuntu_24.04")
		self.assertEqual(caps.images[0].provider_metadata["version"], "24.04 LTS (Noble Numbat)")


_DEFAULT_SCHEMA_TWO_DISK = {
	"disks": [
		{"device": "/dev/nvme0n1", "partitions": [{"label": "uefi", "number": 1, "size": 536870912}]},
		{"device": "/dev/nvme1n1", "partitions": []},
	],
	"raids": [],
	"filesystems": [],
}


class TestScalewayProviderProvision(IntegrationTestCase):
	def test_provision_assembles_install_and_returns_partial(self) -> None:
		provider = _build_provider()
		provider.client.create_server.return_value = {"id": "srv-uuid", "status": "delivering"}
		provider.client.get_default_partitioning_schema.return_value = _DEFAULT_SCHEMA_TWO_DISK
		# No v6 flexible IP yet → provision allocates + attaches one.
		provider.client.list_flexible_ips.return_value = []
		provider.client.create_flexible_ip.return_value = {
			"id": "fip-v6",
			"ip_address": "2001:bc8:abcd:1::/64",
		}
		request = ProvisionRequest(
			title="atlas-srv-1",
			size="Scaleway/EM-A610R-NVMe",
			image="Scaleway/Ubuntu_24.04",
			ssh_key=SshKey(vendor_id="ssh-key-uuid"),
			networking=Networking.DUAL_STACK,
			tags=("atlas", "atlas-srv-1"),
		)
		with (
			patch.object(provider, "_resolve_offer_id", return_value="offer-uuid-1"),
			patch.object(provider, "_resolve_os_id", return_value="os-uuid-1"),
		):
			result = provider.provision(request)
		_, kwargs = provider.client.create_server.call_args
		self.assertEqual(kwargs["offer_id"], "offer-uuid-1")
		self.assertEqual(kwargs["project_id"], "proj-uuid")
		self.assertEqual(kwargs["name"], "atlas-srv-1")
		self.assertEqual(kwargs["tags"], ["atlas", "atlas-srv-1"])
		self.assertEqual(kwargs["install"]["os_id"], "os-uuid-1")
		self.assertEqual(kwargs["install"]["hostname"], "atlas-srv-1")
		self.assertEqual(kwargs["install"]["ssh_key_ids"], ["ssh-key-uuid"])
		# The RAID partitioning schema is built from the vendor default's device
		# names and passed inline with the install.
		schema = kwargs["install"]["partitioning_schema"]
		self.assertEqual([d["device"] for d in schema["disks"]], ["/dev/nvme0n1", "/dev/nvme1n1"])
		self.assertEqual([r["name"] for r in schema["raids"]], ["/dev/md0", "/dev/md1", "/dev/md2"])
		# No cloud_init by default — Atlas bootstraps over SSH.
		self.assertIsNone(kwargs["user_data"])
		self.assertEqual(result.provider_resource_id, "srv-uuid")
		self.assertEqual(result.size, "Scaleway/EM-A610R-NVMe")
		self.assertFalse(result.ready)
		# A routed flexible v6 /64 is allocated + attached to the new server (the
		# bundled subnet is on-link, not a VM range).
		provider.client.create_flexible_ip.assert_called_once_with(project_id="proj-uuid", is_ipv6=True)
		provider.client.attach_flexible_ip.assert_called_once_with("fip-v6", "srv-uuid")

	def test_provision_reuses_existing_flexible_v6(self) -> None:
		"""If the server already holds a v6 flexible IP (a re-provision), don't
		stack a second — reuse it."""
		provider = _build_provider()
		provider.client.create_server.return_value = {"id": "srv-uuid", "status": "delivering"}
		provider.client.get_default_partitioning_schema.return_value = _DEFAULT_SCHEMA_TWO_DISK
		provider.client.list_flexible_ips.return_value = [
			{"id": "fip-v6-existing", "ip_address": "2001:bc8:ffff:9::/64", "server_id": "srv-uuid"}
		]
		request = ProvisionRequest(
			title="atlas-srv-3",
			size="Scaleway/EM-A610R-NVMe",
			image="Scaleway/Ubuntu_24.04",
			ssh_key=SshKey(vendor_id="ssh-key-uuid"),
		)
		with (
			patch.object(provider, "_resolve_offer_id", return_value="offer-uuid-1"),
			patch.object(provider, "_resolve_os_id", return_value="os-uuid-1"),
		):
			provider.provision(request)
		provider.client.create_flexible_ip.assert_not_called()

	def test_provision_registers_ssh_key_when_no_match_exists(self) -> None:
		"""Only a body, no cached vendor_id, and IAM holds no matching key → register
		it once and install the new id."""
		provider = _build_provider()
		provider.client.create_server.return_value = {"id": "srv-uuid", "status": "delivering"}
		provider.client.get_default_partitioning_schema.return_value = _DEFAULT_SCHEMA_TWO_DISK
		provider.client.list_ssh_keys.return_value = []
		provider.client.register_ssh_key.return_value = {"id": "new-key-uuid"}
		provider.client.list_flexible_ips.return_value = []
		provider.client.create_flexible_ip.return_value = {
			"id": "fip-v6",
			"ip_address": "2001:bc8:abcd:1::/64",
		}
		request = ProvisionRequest(
			title="atlas-srv-2",
			size="Scaleway/EM-A610R-NVMe",
			image="Scaleway/Ubuntu_24.04",
			ssh_key=SshKey(public_key="ssh-ed25519 AAAA... user@host"),
		)
		with (
			patch.object(provider, "_resolve_offer_id", return_value="offer-uuid-1"),
			patch.object(provider, "_resolve_os_id", return_value="os-uuid-1"),
		):
			provider.provision(request)
		provider.client.register_ssh_key.assert_called_once()
		_, kwargs = provider.client.create_server.call_args
		self.assertEqual(kwargs["install"]["ssh_key_ids"], ["new-key-uuid"])

	def test_provision_reuses_matching_iam_key_without_registering(self) -> None:
		"""Only a body, no cached vendor_id, but IAM already holds a key with the
		same body (a prior provision) → reuse that id, do NOT register a duplicate.
		The trailing comment differs to prove the match is comment-agnostic."""
		provider = _build_provider()
		provider.client.create_server.return_value = {"id": "srv-uuid", "status": "delivering"}
		provider.client.get_default_partitioning_schema.return_value = _DEFAULT_SCHEMA_TWO_DISK
		provider.client.list_ssh_keys.return_value = [
			{"id": "unrelated-key", "public_key": "ssh-ed25519 ZZZZ other@host"},
			{"id": "matching-key", "public_key": "ssh-ed25519 AAAA different-comment"},
		]
		provider.client.list_flexible_ips.return_value = []
		provider.client.create_flexible_ip.return_value = {
			"id": "fip-v6",
			"ip_address": "2001:bc8:abcd:1::/64",
		}
		request = ProvisionRequest(
			title="atlas-srv-4",
			size="Scaleway/EM-A610R-NVMe",
			image="Scaleway/Ubuntu_24.04",
			ssh_key=SshKey(public_key="ssh-ed25519 AAAA user@host"),
		)
		with (
			patch.object(provider, "_resolve_offer_id", return_value="offer-uuid-1"),
			patch.object(provider, "_resolve_os_id", return_value="os-uuid-1"),
		):
			provider.provision(request)
		provider.client.register_ssh_key.assert_not_called()
		_, kwargs = provider.client.create_server.call_args
		self.assertEqual(kwargs["install"]["ssh_key_ids"], ["matching-key"])


class TestScalewayProviderDescribe(IntegrationTestCase):
	def test_describe_partial_when_delivering(self) -> None:
		provider = _build_provider()
		provider.client.get_server.return_value = {
			"id": "srv-uuid",
			"status": "delivering",
			"offer_name": "EM-A610R-NVMe",
			"install": {"status": "to_install"},
		}
		result = provider.describe("srv-uuid")
		self.assertFalse(result.ready)
		self.assertEqual(result.size, "Scaleway/EM-A610R-NVMe")

	def test_describe_partial_when_installed_but_not_ready(self) -> None:
		provider = _build_provider()
		provider.client.get_server.return_value = {
			"id": "srv-uuid",
			"status": "delivering",
			"install": {"status": "completed"},
		}
		# ready requires status==ready AND install.status==completed.
		self.assertFalse(provider.describe("srv-uuid").ready)

	def test_describe_ready_uses_flexible_v6_as_vm_range(self) -> None:
		provider = _build_provider()
		provider.client.get_server.return_value = {
			"id": "srv-uuid",
			"status": "ready",
			"offer_name": "EM-A610R-NVMe",
			"install": {"status": "completed"},
			"ips": [
				{"version": "IPv4", "address": "51.15.1.2"},
				{"version": "IPv6", "address": "2001:bc8:1234:5678::1", "prefix_length": 64},
			],
		}
		# The routed VM range is the ATTACHED flexible v6 /64 — distinct from the
		# host's on-link bundled subnet. The whole /64 is the range (no carve).
		provider.client.list_flexible_ips.return_value = [
			{"id": "fip-v6", "ip_address": "2001:bc8:9999:abc::/64", "server_id": "srv-uuid"},
			{"id": "fip-v4", "ip_address": "51.15.9.9/32", "server_id": "srv-uuid"},
		]
		result = provider.describe("srv-uuid")
		self.assertTrue(result.ready)
		self.assertEqual(result.networking.ipv4_address, "51.15.1.2")
		self.assertEqual(result.networking.ipv6_address, "2001:bc8:1234:5678::1")
		# Host's own subnet from the bundled IP; VM range is the flexible /64.
		self.assertEqual(result.networking.ipv6_prefix, "2001:bc8:1234:5678::/64")
		self.assertEqual(result.networking.ipv6_virtual_machine_range, "2001:bc8:9999:abc::/64")

	def test_describe_ready_falls_back_to_bundled_prefix_without_flexible_v6(self) -> None:
		"""A host with no flexible v6 attached (predates the allocation) still
		describes — the VM range falls back to the bundled prefix."""
		provider = _build_provider()
		provider.client.get_server.return_value = {
			"id": "srv-uuid",
			"status": "ready",
			"install": {"status": "completed"},
			"ips": [
				{"version": "IPv4", "address": "51.15.1.2"},
				{"version": "IPv6", "address": "2001:bc8:1234:5678::1", "prefix_length": 64},
			],
		}
		provider.client.list_flexible_ips.return_value = []
		result = provider.describe("srv-uuid")
		self.assertEqual(result.networking.ipv6_virtual_machine_range, "2001:bc8:1234:5678::/64")

	def test_describe_raises_on_terminal_status(self) -> None:
		provider = _build_provider()
		provider.client.get_server.return_value = {
			"id": "srv-uuid",
			"status": "out_of_stock",
			"install": {"status": "error"},
		}
		with self.assertRaises(ProviderError):
			provider.describe("srv-uuid")


class TestScalewayProviderDestroy(IntegrationTestCase):
	def test_destroy_calls_delete_server(self) -> None:
		provider = _build_provider()
		provider.client.list_flexible_ips.return_value = []
		provider.destroy("srv-uuid")
		provider.client.delete_server.assert_called_once_with("srv-uuid")


class TestScalewayProviderListServers(IntegrationTestCase):
	def test_list_servers_maps_each_payload(self) -> None:
		provider = _build_provider()
		provider.client.list_servers.return_value = [
			{
				"id": "srv-aaa",
				"name": "fr-par-2-bench-01",
				"offer_name": "EM-A610R-NVMe",
				"ips": [{"version": "IPv4", "address": "51.159.1.2"}],
			},
			{
				"id": "srv-bbb",
				"name": "my-scaleway-box",
				"offer_name": "EM-B112X-SSD",
				"ips": [{"version": "IPv4", "address": "62.210.3.4"}],
			},
		]
		discovered = provider.list_servers()
		self.assertEqual(len(discovered), 2)
		first = discovered[0]
		self.assertEqual(first.provider_resource_id, "srv-aaa")
		self.assertEqual(first.title, "fr-par-2-bench-01")
		self.assertEqual(first.ipv4_address, "51.159.1.2")
		# Size label mirrors describe()'s Scaleway/<offer_name> form.
		self.assertEqual(first.size, "Scaleway/EM-A610R-NVMe")

	def test_list_servers_tolerates_box_without_ipv4(self) -> None:
		"""A delivering box may have no IPv4 yet — `public_ipv4` raises there, so
		discovery must yield ipv4=None for it, not break the whole list."""
		provider = _build_provider()
		provider.client.list_servers.return_value = [
			{"id": "srv-new", "name": "delivering-box", "offer_name": "EM-A610R-NVMe", "ips": []},
		]
		discovered = provider.list_servers()
		self.assertEqual(len(discovered), 1)
		self.assertIsNone(discovered[0].ipv4_address)
		self.assertEqual(discovered[0].provider_resource_id, "srv-new")

	def test_list_servers_empty_account(self) -> None:
		provider = _build_provider()
		provider.client.list_servers.return_value = []
		self.assertEqual(provider.list_servers(), ())


class TestScalewayProviderReservedIp(IntegrationTestCase):
	def test_allocate_maps_fip_id_not_address(self) -> None:
		provider = _build_provider()
		provider.client.create_flexible_ip.return_value = {
			"id": "fip-uuid",
			"ip_address": "51.15.9.9/32",
			"server_id": None,
		}
		reserved = provider.allocate_reserved_ip()
		provider.client.create_flexible_ip.assert_called_once_with(project_id="proj-uuid", is_ipv6=False)
		# Unlike DO, the vendor handle is the FIP UUID, not the address.
		self.assertEqual(reserved.ip_address, "51.15.9.9")
		self.assertEqual(reserved.provider_resource_id, "fip-uuid")
		self.assertIsNone(reserved.droplet_resource_id)

	def test_assign_passes_server_id(self) -> None:
		provider = _build_provider()
		provider.assign_reserved_ip("fip-uuid", "srv-uuid")
		provider.client.attach_flexible_ip.assert_called_once_with("fip-uuid", "srv-uuid")

	def test_unassign_calls_detach(self) -> None:
		provider = _build_provider()
		provider.unassign_reserved_ip("fip-uuid")
		provider.client.detach_flexible_ip.assert_called_once_with("fip-uuid")

	def test_release_calls_delete(self) -> None:
		provider = _build_provider()
		provider.release_reserved_ip("fip-uuid")
		provider.client.delete_flexible_ip.assert_called_once_with("fip-uuid")

	def test_list_maps_attached_server(self) -> None:
		provider = _build_provider()
		provider.client.list_flexible_ips.return_value = [
			{"id": "fip-1", "ip_address": "51.15.9.9/32", "server_id": "srv-uuid"},
			{"id": "fip-2", "ip_address": "51.15.9.10/32", "server_id": None},
		]
		reserved = provider.list_reserved_ips()
		self.assertEqual(reserved[0].provider_resource_id, "fip-1")
		self.assertEqual(reserved[0].droplet_resource_id, "srv-uuid")
		self.assertIsNone(reserved[1].droplet_resource_id)

	def test_list_reserved_ips_skips_ipv6_blocks(self) -> None:
		"""list_reserved_ips is inbound-v4 only — a v6 flexible /64 (a VM range)
		must not appear as a reserved IP."""
		provider = _build_provider()
		provider.client.list_flexible_ips.return_value = [
			{"id": "fip-v4", "ip_address": "51.15.9.9/32", "server_id": "srv-uuid"},
			{"id": "fip-v6", "ip_address": "2001:bc8:9999:abc::/64", "server_id": "srv-uuid"},
		]
		reserved = provider.list_reserved_ips()
		self.assertEqual([r.provider_resource_id for r in reserved], ["fip-v4"])

	def test_destroy_releases_attached_flexible_ips(self) -> None:
		"""destroy() releases the server's flexible IPs (v6 VM range + any v4)
		before deleting the server, so nothing leaks."""
		provider = _build_provider()
		provider.client.list_flexible_ips.return_value = [
			{"id": "fip-v6", "ip_address": "2001:bc8:9::/64", "server_id": "srv-uuid"},
			{"id": "fip-v4", "ip_address": "51.15.9.9/32", "server_id": "srv-uuid"},
			{"id": "fip-other", "ip_address": "51.15.9.10/32", "server_id": "other-srv"},
		]
		provider.destroy("srv-uuid")
		deleted = {call.args[0] for call in provider.client.delete_flexible_ip.call_args_list}
		self.assertEqual(deleted, {"fip-v6", "fip-v4"})
		provider.client.delete_server.assert_called_once_with("srv-uuid")


class TestBuildRaidPartitioningSchema(IntegrationTestCase):
	"""The pure RAID-1 schema builder — device list in, schema dict out."""

	def test_builds_symmetric_aligned_raid1_layout(self) -> None:
		from atlas.atlas.providers.scaleway import build_raid_partitioning_schema

		schema = build_raid_partitioning_schema(["/dev/nvme0n1", "/dev/nvme1n1"])
		# Both disks get the IDENTICAL 4-partition table so numbers align — incl.
		# the buffer uefi on disk 1 (only disk 0's ESP is mounted).
		labels = [[p["label"] for p in d["partitions"]] for d in schema["disks"]]
		self.assertEqual(labels, [["uefi", "boot", "root", "data"], ["uefi", "boot", "root", "data"]])
		# data uses all remaining space on both disks; root is the fixed 64 GiB.
		for disk in schema["disks"]:
			data = disk["partitions"][3]
			self.assertTrue(data["use_all_available_space"])
			self.assertNotIn("size", data)
			self.assertEqual(disk["partitions"][2]["size"], 64 * 1024 * 1024 * 1024)

	def test_raid1_arrays_mirror_matching_partition_numbers(self) -> None:
		from atlas.atlas.providers.scaleway import build_raid_partitioning_schema

		schema = build_raid_partitioning_schema(["/dev/nvme0n1", "/dev/nvme1n1"])
		raids = {r["name"]: r for r in schema["raids"]}
		self.assertEqual(set(raids), {"/dev/md0", "/dev/md1", "/dev/md2"})
		for raid in raids.values():
			self.assertEqual(raid["level"], "raid_level_1")
		# md0=boot(p2), md1=root(p3), md2=data(p4) — each across both disks.
		self.assertEqual(raids["/dev/md0"]["devices"], ["/dev/nvme0n1p2", "/dev/nvme1n1p2"])
		self.assertEqual(raids["/dev/md1"]["devices"], ["/dev/nvme0n1p3", "/dev/nvme1n1p3"])
		self.assertEqual(raids["/dev/md2"]["devices"], ["/dev/nvme0n1p4", "/dev/nvme1n1p4"])

	def test_data_raid_is_left_raw_for_the_lvm_pool(self) -> None:
		from atlas.atlas.providers.scaleway import build_raid_partitioning_schema

		schema = build_raid_partitioning_schema(["/dev/nvme0n1", "/dev/nvme1n1"])
		# ESP on disk 0, ext4 on md0 (/boot) + md1 (/) — but md2 (data) is NOT
		# formatted: it is left raw so ThinPool consumes it as the PV.
		mounts = {f["device"]: f["mountpoint"] for f in schema["filesystems"]}
		self.assertEqual(mounts["/dev/nvme0n1p1"], "/boot/efi")
		self.assertEqual(mounts["/dev/md0"], "/boot")
		self.assertEqual(mounts["/dev/md1"], "/")
		self.assertNotIn("/dev/md2", mounts)

	def test_sd_style_device_names_have_no_p_separator(self) -> None:
		from atlas.atlas.providers.scaleway import build_raid_partitioning_schema

		# sd/vd drives partition as /dev/sda1 (no `p`); nvme as /dev/nvme0n1p1.
		schema = build_raid_partitioning_schema(["/dev/sda", "/dev/sdb"])
		self.assertEqual(schema["raids"][0]["devices"], ["/dev/sda2", "/dev/sdb2"])
		self.assertEqual(schema["filesystems"][0]["device"], "/dev/sda1")

	def test_only_first_two_disks_are_mirrored(self) -> None:
		from atlas.atlas.providers.scaleway import build_raid_partitioning_schema

		schema = build_raid_partitioning_schema(["/dev/nvme0n1", "/dev/nvme1n1", "/dev/nvme2n1"])
		self.assertEqual([d["device"] for d in schema["disks"]], ["/dev/nvme0n1", "/dev/nvme1n1"])


class TestScalewayPartitioningSchemaFallback(IntegrationTestCase):
	"""_build_partitioning_schema degrades to the vendor default (None) rather than
	failing the provision when custom partitioning isn't possible."""

	def test_returns_none_when_endpoint_unavailable(self) -> None:
		from atlas.atlas.scaleway import ScalewayError

		provider = _build_provider()
		provider.client.get_default_partitioning_schema.side_effect = ScalewayError("404 not found")
		self.assertIsNone(provider._build_partitioning_schema("offer", "os"))

	def test_returns_none_with_single_disk_offer(self) -> None:
		provider = _build_provider()
		provider.client.get_default_partitioning_schema.return_value = {
			"disks": [{"device": "/dev/sda", "partitions": []}],
		}
		# RAID-1 needs a mirror pair — a one-disk box falls back to the default.
		self.assertIsNone(provider._build_partitioning_schema("offer", "os"))


class TestScalewayClientMapping(IntegrationTestCase):
	"""Pure mapping helpers — no HTTP, no provider."""

	def test_public_ipv6_defaults_to_64(self) -> None:
		from atlas.atlas.scaleway import public_ipv6

		address, prefix = public_ipv6({"id": "x", "ips": [{"version": "IPv6", "address": "2001:bc8:1::1"}]})
		self.assertEqual(address, "2001:bc8:1::1")
		self.assertEqual(prefix, "2001:bc8:1::/64")

	def test_public_ipv6_parses_suffix(self) -> None:
		from atlas.atlas.scaleway import public_ipv6

		address, prefix = public_ipv6(
			{"id": "x", "ips": [{"version": "IPv6", "address": "2001:bc8:2::5/64"}]}
		)
		self.assertEqual(address, "2001:bc8:2::5")
		self.assertEqual(prefix, "2001:bc8:2::/64")

	def test_money_to_int_rounds(self) -> None:
		from atlas.atlas.providers.scaleway import _money_to_int

		self.assertEqual(_money_to_int({"units": 39, "nanos": 990000000}), 40)
		self.assertEqual(_money_to_int({"units": 134, "nanos": 0}), 134)
		self.assertIsNone(_money_to_int({}))


class TestScalewayClientHttp(IntegrationTestCase):
	"""HTTP-layer contract tests — mock the transport (`_raw_request`) so the
	response-unwrapping the provider tests bypass (they mock the whole client) is
	exercised against the SHAPE the live API actually returns."""

	def _client(self):
		from atlas.atlas.scaleway import ScalewayClient

		return ScalewayClient(secret_key="scw-secret", zone="fr-par-2")

	def _response(self, status: int, payload: dict):
		"""A stand-in requests.Response: status + .json() + truthy .content."""
		return SimpleNamespace(status_code=status, content=b"x", json=lambda: payload)

	def test_register_ssh_key_returns_top_level_resource(self) -> None:
		"""IAM returns the created key UNWRAPPED (id/public_key at top level), not
		inside an `{"ssh_key": ...}` envelope. The live API raised KeyError when we
		assumed the envelope — this pins the real shape."""
		client = self._client()
		api_payload = {"id": "key-uuid-123", "name": "atlas-x", "public_key": "ssh-ed25519 AAAA x"}
		with patch.object(client, "_raw_request", return_value=self._response(200, api_payload)):
			created = client.register_ssh_key(name="atlas-x", public_key="ssh-ed25519 AAAA x", project_id="p")
		self.assertEqual(created["id"], "key-uuid-123")

	def test_get_default_partitioning_schema_passes_offer_and_os(self) -> None:
		"""The default-schema call is GET /partitioning-schemas/default with the
		offer_id + os_id as query params, returning the bare schema object."""
		client = self._client()
		payload = {"disks": [{"device": "/dev/nvme0n1"}], "raids": [], "filesystems": []}
		with patch.object(client, "_raw_request", return_value=self._response(200, payload)) as raw:
			schema = client.get_default_partitioning_schema("offer-uuid", "os-uuid")
		method, path = raw.call_args.args[0], raw.call_args.args[1]
		self.assertEqual(method, "GET")
		self.assertIn("/partitioning-schemas/default", path)
		self.assertIn("offer_id=offer-uuid", path)
		self.assertIn("os_id=os-uuid", path)
		self.assertEqual(schema["disks"][0]["device"], "/dev/nvme0n1")

	def test_register_ssh_key_unwraps_legacy_envelope(self) -> None:
		"""If a future/legacy response DOES wrap the key in `{"ssh_key": ...}`, still
		unwrap it — the handler tolerates both shapes."""
		client = self._client()
		with patch.object(
			client, "_raw_request", return_value=self._response(200, {"ssh_key": {"id": "wrapped"}})
		):
			created = client.register_ssh_key(name="a", public_key="b", project_id="p")
		self.assertEqual(created["id"], "wrapped")

	def test_list_servers_is_unfiltered_zone_scoped(self) -> None:
		"""Unfiltered list (discover/import): GET /servers?page_size=100 in the zone,
		no `tags=` filter, returning the `servers` array."""
		client = self._client()
		payload = {"servers": [{"id": "srv-1"}, {"id": "srv-2"}]}
		with patch.object(client, "_raw_request", return_value=self._response(200, payload)) as raw:
			servers = client.list_servers()
		method, path = raw.call_args.args[0], raw.call_args.args[1]
		self.assertEqual(method, "GET")
		self.assertIn("/baremetal/v1/zones/fr-par-2/servers", path)
		self.assertNotIn("tags=", path)
		self.assertEqual([s["id"] for s in servers], ["srv-1", "srv-2"])
