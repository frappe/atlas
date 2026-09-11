from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from string import Template
from unittest.mock import patch

import bcrypt
from frappe.tests import UnitTestCase
from frappe.utils import now_datetime

import atlas.service.core.proxy.configuration as configuration_module
from atlas.service.core.proxy.configuration import (
	APPLY_COMMAND,
	CONFIG_PATH,
	DAEMON_SOCKET_UNIT,
	DAEMON_UNIT,
	ProxyConfiguration,
)

CERTIFICATE = "-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----"
PRIVATE_KEY = "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----"


class _FakeSettings:
	def __init__(self) -> None:
		self.wildcard_domain = "par-1.example.com"
		self.region_id = 1
		self.central_jwks_url = "https://issuer.example.com/jwks.json"
		self.jwks_url = "https://atlas.example.com/api/atlas/jwks.json"
		self.wildcard_tls_expires_on = None
		self.proxy_cluster_password_rotated_on = now_datetime() - timedelta(minutes=5)
		self.passwords = {
			"wildcard_tls_certificate": CERTIFICATE,
			"wildcard_tls_private_key": PRIVATE_KEY,
			"proxy_cluster_password": "a-control-password",
			"previous_proxy_cluster_password": "previous-control-password",
		}

	@property
	def proxy_audience_id(self) -> str:
		return f"atlas-proxy:{self.region_id}"

	@property
	def issuer(self) -> str:
		return f"atlas:{self.region_id}"

	def get_password(self, fieldname: str, raise_exception: bool = True) -> str | None:
		return self.passwords.get(fieldname)


class _FakeProxyServer:
	name = "proxy-001"

	def __init__(self, password: str = "a-control-password") -> None:
		self.password = password

	def get_password(self, fieldname: str, raise_exception: bool = True) -> str | None:
		return self.password

	def get_domain(self) -> str:
		return "proxy-001.par-1.example.com"


def _build(settings: _FakeSettings | None = None, password: str = "a-control-password"):
	settings = settings or _FakeSettings()
	settings.passwords["proxy_cluster_password"] = password
	patched = patch("atlas.service.core.proxy.configuration.frappe.get_single", return_value=settings)
	patched.start()
	proxy_server = _FakeProxyServer(password)
	configuration = ProxyConfiguration(proxy_server, [proxy_server])
	patched.stop()
	return configuration


class TestProxyConfiguration(UnitTestCase):
	def test_the_file_is_valid_toml_with_every_section(self) -> None:
		document = tomllib.loads(_build().content)

		self.assertEqual(document["control"]["domain"], "proxy.par-1.example.com")
		self.assertEqual(document["control"]["node_domain"], "proxy-001.par-1.example.com")
		self.assertEqual(document["auto_proxy"]["address_prefix"], "fdaa:1")
		self.assertEqual(document["auto_proxy"]["host_prefixes"], ["site-", "*-vm-"])
		self.assertEqual(document["tls"]["wildcard_domain"], "*.par-1.example.com")
		self.assertEqual(document["tls"]["fullchain_pem"].strip(), CERTIFICATE)
		self.assertEqual(document["tls"]["private_key_pem"].strip(), PRIVATE_KEY)
		self.assertEqual(document["auth"]["jwks_audience_id"], "atlas-proxy:1")
		self.assertEqual(document["auth"]["jwks_issuers"], ["central", "atlas:1"])
		self.assertTrue(bcrypt.checkpw(b"a-control-password", document["auth"]["password_hash"].encode()))
		self.assertEqual(document["cluster"]["node_id"], "proxy-001")
		self.assertEqual(document["cluster"]["password"], "a-control-password")
		self.assertEqual(document["cluster"]["peers"][0]["node_id"], "proxy-001")

	def test_the_file_configures_public_and_cluster_passwords(self) -> None:
		configuration = _build(password="a-control-password")

		document = tomllib.loads(configuration.content)

		self.assertTrue(bcrypt.checkpw(b"a-control-password", document["auth"]["password_hash"].encode()))
		self.assertTrue(
			bcrypt.checkpw(b"previous-control-password", document["auth"]["previous_password_hash"].encode())
		)
		self.assertGreater(document["auth"]["previous_password_valid_until"], 0)
		self.assertEqual(
			document["cluster"]["previous_password_valid_until"],
			document["auth"]["previous_password_valid_until"],
		)
		self.assertEqual(
			document["auth"]["jwks_url"],
			"https://atlas.example.com/api/atlas/jwks.json",
		)
		self.assertEqual(document["auth"]["jwks_audience_id"], "atlas-proxy:1")
		self.assertEqual(document["cluster"]["password"], "a-control-password")
		self.assertEqual(document["cluster"]["previous_password"], "previous-control-password")

	def test_the_previous_password_expires_10_minutes_after_rotation(self) -> None:
		settings = _FakeSettings()
		settings.proxy_cluster_password_rotated_on = datetime(2026, 9, 8, 12, 0)

		with patch.object(configuration_module, "get_system_timezone", return_value="UTC"):
			valid_until = _build(settings).previous_password_valid_until

		expected = int(datetime(2026, 9, 8, 12, 10, tzinfo=UTC).timestamp())
		self.assertEqual(valid_until, expected)

	def test_a_missing_certificate_is_refused(self) -> None:
		settings = _FakeSettings()
		settings.passwords = {}

		with self.assertRaises(Exception):
			_build(settings).content

	def test_a_missing_cluster_password_is_refused(self) -> None:
		with self.assertRaises(Exception):
			_build(password="").content

	def test_the_content_and_digest_are_stable_while_the_inputs_are(self) -> None:
		configuration = _build()

		self.assertEqual(configuration.content, configuration.content)
		self.assertEqual(configuration.digest, configuration.digest)

	def test_the_digest_follows_a_changed_certificate(self) -> None:
		before = _build().digest

		settings = _FakeSettings()
		settings.passwords["wildcard_tls_certificate"] = "-----BEGIN CERTIFICATE-----\nnew\n-----"

		self.assertNotEqual(before, _build(settings).digest)

	def test_the_digest_follows_the_template(self) -> None:
		configuration = _build()
		before = configuration.digest

		with patch.object(configuration_module, "CONFIG_TEMPLATE", Template('[control]\ndomain = "$domain"')):
			self.assertNotEqual(before, _build().digest)

	def test_the_digest_follows_a_changed_password(self) -> None:
		before = _build(password="first").digest

		self.assertNotEqual(before, _build(password="second").digest)

	def test_the_write_command_installs_the_file_with_a_private_mode(self) -> None:
		command = _build().get_write_command()

		self.assertIn(f"install -m 0600 /dev/null {CONFIG_PATH}.tmp", command)
		self.assertIn(f"cat > {CONFIG_PATH}.tmp <<'ATLAS_PROXY_CONFIG_END'", command)
		self.assertTrue(command.rstrip().endswith(f"mv -f {CONFIG_PATH}.tmp {CONFIG_PATH}"))

	# The task records its command, so it must not contain a secret.
	def test_the_apply_command_holds_no_secret(self) -> None:
		command = _build().get_apply_command()

		self.assertIn(APPLY_COMMAND, command)
		self.assertNotIn(CERTIFICATE, command)
		self.assertNotIn(PRIVATE_KEY, command)
		self.assertNotIn("cluster_password", command)

	# The setup script leaves the daemon stopped, so this step has to start it.
	def test_the_apply_command_starts_the_daemon(self) -> None:
		command = _build().get_apply_command()

		self.assertIn(f"systemctl enable --now {DAEMON_SOCKET_UNIT}", command)
		self.assertIn(f"systemctl enable {DAEMON_UNIT}", command)
		self.assertIn(f"systemctl restart {DAEMON_UNIT}", command)
		self.assertLess(
			command.index(f"enable --now {DAEMON_SOCKET_UNIT}"),
			command.index(f"restart {DAEMON_UNIT}"),
		)
		self.assertLess(command.index(APPLY_COMMAND), command.index(f"enable {DAEMON_UNIT}"))


class TestPushToActiveProxies(UnitTestCase):
	def test_only_active_proxies_are_queued(self) -> None:
		import atlas.service.core.proxy.configuration as configuration

		with (
			patch.object(configuration.frappe, "get_all", return_value=["proxy-001"]) as get_all,
			patch.object(configuration.frappe, "enqueue_doc") as enqueue_doc,
		):
			configuration.push_configuration_to_active_proxies()

		self.assertEqual(get_all.call_args.kwargs["filters"], {"status": "Active"})
		self.assertEqual(enqueue_doc.call_args.args[:2], ("Proxy Server", "proxy-001"))
		self.assertEqual(enqueue_doc.call_args.args[2], "_push_configuration")
