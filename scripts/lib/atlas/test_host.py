"""Unit tests for the pure host-layout / identity / env-parsing helpers.

Run with bare `python3 -m unittest atlas.test_host` from scripts/lib: no Frappe,
no site, no droplet, no host. These cover the on-host path derivation (the
double-UUID jail nesting + the 108-byte socket workaround), the per-VM identity
derivation, and the network.env parse-and-guard — all things that, as shell,
could only be eyeballed on a real droplet.
"""

import unittest

from atlas.network_env import NetworkEnv, read_network_env_optional
from atlas.paths import SUN_PATH_MAX, VirtualMachinePaths, image_directory
from atlas.rootfs import Identity

UUID = "d4f7c1a2-1111-2222-3333-444455556666"


class TestVirtualMachinePaths(unittest.TestCase):
    def setUp(self):
        self.paths = VirtualMachinePaths(UUID)

    def test_jail_root_nests_uuid_twice(self):
        # <dir>/jail/firecracker/<uuid>/root — the chroot layout the jailer wants.
        self.assertEqual(
            self.paths.jail_root,
            f"/var/lib/atlas/virtual-machines/{UUID}/jail/firecracker/{UUID}/root",
        )

    def test_rootfs_node_under_jail_root(self):
        self.assertEqual(self.paths.rootfs_node, f"{self.paths.jail_root}/rootfs.ext4")

    def test_kernel_and_config_under_jail_root(self):
        self.assertEqual(self.paths.kernel, f"{self.paths.jail_root}/vmlinux")
        self.assertEqual(self.paths.firecracker_config, f"{self.paths.jail_root}/firecracker.json")

    def test_chroot_base_is_jail_dir(self):
        # What the jailer's --chroot-base-dir points at: <dir>/jail.
        self.assertTrue(self.paths.jail_root.startswith(self.paths.jail_chroot_base + "/"))
        self.assertEqual(self.paths.jail_chroot_base, f"{self.paths.directory}/jail")

    def test_systemd_unit_is_instance(self):
        self.assertEqual(self.paths.systemd_unit, f"firecracker-vm@{UUID}.service")

    def test_network_env_under_vm_directory(self):
        self.assertEqual(self.paths.network_env, f"{self.paths.directory}/network.env")

    def test_api_socket_relative_name_dodges_sun_path_limit(self):
        # The absolute socket path blows past AF_UNIX's 108-byte sun_path limit,
        # which is why callers cd into its dir and use the short relative name.
        self.assertGreater(len(self.paths.api_socket), SUN_PATH_MAX)
        self.assertEqual(self.paths.api_socket_name, "firecracker.socket")
        self.assertTrue(
            self.paths.api_socket.startswith(self.paths.api_socket_directory + "/")
        )

    def test_image_directory(self):
        self.assertEqual(image_directory("ubuntu-24"), "/var/lib/atlas/images/ubuntu-24")


class TestIdentity(unittest.TestCase):
    def setUp(self):
        self.identity = Identity(
            uuid=UUID,
            ipv6_address="2001:db8::2",
            ssh_public_key="ssh-ed25519 AAA",
            ipv4_guest_cidr="100.64.0.10/30",
            ipv4_gateway="100.64.0.9",
        )

    def test_hostname_is_uuid_prefix(self):
        self.assertEqual(self.identity.hostname, "atlas-d4f7c1a2")

    def test_machine_id_is_32_hex(self):
        # The shell's `tr -d '-' | head -c 32`: 32 lowercase hex, no dashes.
        self.assertEqual(self.identity.machine_id, UUID.replace("-", "")[:32])
        self.assertEqual(len(self.identity.machine_id), 32)
        self.assertNotIn("-", self.identity.machine_id)


class TestNetworkEnv(unittest.TestCase):
    SAMPLE = (
        "TAP_DEVICE=atlas-x\n"
        "ATLAS_FC_UID=247312\n"
        "# a comment\n"
        "\n"
        "VIRTUAL_MACHINE_IPV6=2001:db8::2\n"
    )

    def test_parse_skips_comments_and_blanks(self):
        env = NetworkEnv.parse(self.SAMPLE)
        self.assertEqual(env.require("TAP_DEVICE"), "atlas-x")
        self.assertEqual(env.require("VIRTUAL_MACHINE_IPV6"), "2001:db8::2")

    def test_require_int_coerces(self):
        env = NetworkEnv.parse(self.SAMPLE)
        self.assertEqual(env.require_int("ATLAS_FC_UID"), 247312)

    def test_require_missing_fails_loud_naming_var(self):
        env = NetworkEnv.parse(self.SAMPLE)
        with self.assertRaises(SystemExit) as caught:
            env.require("NOPE")
        self.assertIn("NOPE", str(caught.exception))

    def test_require_int_bad_value_fails_loud(self):
        env = NetworkEnv.parse("ATLAS_FC_UID=notanumber\n")
        with self.assertRaises(SystemExit) as caught:
            env.require_int("ATLAS_FC_UID")
        self.assertIn("ATLAS_FC_UID", str(caught.exception))

    def test_get_returns_default_for_missing(self):
        # The `${VAR:-}` form the down path uses.
        env = NetworkEnv.parse(self.SAMPLE)
        self.assertEqual(env.get("MISSING", "fallback"), "fallback")
        self.assertEqual(env.get("MISSING"), "")

    def test_strips_surrounding_quotes(self):
        env = NetworkEnv.parse('TAP_DEVICE="atlas-x"\n')
        self.assertEqual(env.require("TAP_DEVICE"), "atlas-x")

    def test_optional_read_of_missing_file_is_empty(self):
        # read_network_env_optional tolerates an absent file (terminate-vm raced).
        env = read_network_env_optional("/nonexistent/path/network.env")
        self.assertEqual(env.values, {})
        self.assertEqual(env.get("ANYTHING"), "")


if __name__ == "__main__":
    unittest.main()
