from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.atlas.core.tls.certificate import (
	create_certificate_authority,
	read_certificate,
	verify_issued_certificate,
	verify_key_pair,
)
from atlas.atlas.core.tls.metal import (
	_client_certificate_files,
	atlas_client_identity,
	client_certificate_files,
	ensure_atlas_client_certificate,
	ensure_server_certificate,
)


class TestMetalCertificate(UnitTestCase):
	def test_server_certificate_is_saved_as_a_password(self) -> None:
		ca_certificate, ca_private_key = create_certificate_authority("Atlas Metal CA test")
		credentials = {
			"metal_tls_ca_certificate": ca_certificate,
			"metal_tls_ca_private_key": ca_private_key,
		}
		settings = SimpleNamespace(
			wildcard_domain="example.test",
			get_password=Mock(side_effect=lambda field, **_kwargs: credentials[field]),
		)
		server = SimpleNamespace(
			name="metal-12",
			settings=settings,
			wireguard_ip_address="fdab::12",
			private_ipv4_address="10.0.0.12",
			public_ipv4_address="192.0.2.12",
			get_password=Mock(return_value=None),
			save=Mock(),
		)

		returned_ca, certificate, private_key = ensure_server_certificate(server)

		self.assertEqual(returned_ca, ca_certificate)
		self.assertEqual(server.metald_tls_certificate, certificate)
		self.assertEqual(server.metald_tls_private_key, private_key)
		server.save.assert_called_once_with(ignore_permissions=True, ignore_version=True)

	def test_server_certificate_is_reissued_inside_the_renewal_window(self) -> None:
		ca_certificate, ca_private_key = create_certificate_authority("Atlas Metal CA test")
		credentials = {
			"metal_tls_ca_certificate": ca_certificate,
			"metal_tls_ca_private_key": ca_private_key,
		}
		settings = SimpleNamespace(
			wildcard_domain="example.test",
			get_password=Mock(side_effect=lambda field, **_kwargs: credentials[field]),
		)
		server = SimpleNamespace(
			name="metal-12",
			settings=settings,
			wireguard_ip_address="fdab::12",
			private_ipv4_address="10.0.0.12",
			public_ipv4_address="192.0.2.12",
			get_password=Mock(return_value=None),
			save=Mock(),
		)
		_, current_certificate, current_private_key = ensure_server_certificate(server)
		server.get_password = Mock(
			side_effect=lambda field, **_kwargs: {
				"metald_tls_certificate": current_certificate,
				"metald_tls_private_key": current_private_key,
			}[field]
		)

		with patch("atlas.atlas.core.tls.metal.CERTIFICATE_RENEWAL_WINDOW_DAYS", 100_000):
			_, renewed_certificate, _ = ensure_server_certificate(server)

		self.assertNotEqual(renewed_certificate, current_certificate)
		self.assertEqual(server.metald_tls_certificate, renewed_certificate)
		self.assertIsNotNone(server.metald_tls_expires_on)

	def test_current_server_certificate_is_kept(self) -> None:
		ca_certificate, ca_private_key = create_certificate_authority("Atlas Metal CA test")
		credentials = {
			"metal_tls_ca_certificate": ca_certificate,
			"metal_tls_ca_private_key": ca_private_key,
		}
		settings = SimpleNamespace(
			wildcard_domain="example.test",
			get_password=Mock(side_effect=lambda field, **_kwargs: credentials[field]),
		)
		server = SimpleNamespace(
			name="metal-12",
			settings=settings,
			wireguard_ip_address="fdab::12",
			private_ipv4_address="10.0.0.12",
			public_ipv4_address="192.0.2.12",
			get_password=Mock(return_value=None),
			save=Mock(),
		)
		_, current_certificate, current_private_key = ensure_server_certificate(server)
		server.get_password = Mock(
			side_effect=lambda field, **_kwargs: {
				"metald_tls_certificate": current_certificate,
				"metald_tls_private_key": current_private_key,
			}[field]
		)
		server.save.reset_mock()

		_, kept_certificate, _ = ensure_server_certificate(server)

		self.assertEqual(kept_certificate, current_certificate)
		server.save.assert_not_called()


class TestAtlasClientCertificate(UnitTestCase):
	def test_client_certificate_is_issued_with_the_pinned_identity(self) -> None:
		ca_certificate, ca_private_key = create_certificate_authority("Atlas Metal CA test")
		stored = {
			"metal_tls_ca_certificate": ca_certificate,
			"metal_tls_ca_private_key": ca_private_key,
			"atlas_tls_certificate": None,
			"atlas_tls_private_key": None,
		}
		settings = SimpleNamespace(
			wildcard_domain="example.test",
			get_password=Mock(side_effect=lambda field, **_kwargs: stored[field]),
		)

		self.assertTrue(ensure_atlas_client_certificate(settings))

		self.assertEqual(atlas_client_identity(settings), "atlas.example.test")
		verify_key_pair(settings.atlas_tls_certificate, settings.atlas_tls_private_key)
		verify_issued_certificate(settings.atlas_tls_certificate, ca_certificate)
		self.assertEqual(read_certificate(settings.atlas_tls_certificate).dns_names, ("atlas.example.test",))

	def test_a_current_client_certificate_is_kept(self) -> None:
		ca_certificate, ca_private_key = create_certificate_authority("Atlas Metal CA test")
		stored = {
			"metal_tls_ca_certificate": ca_certificate,
			"metal_tls_ca_private_key": ca_private_key,
			"atlas_tls_certificate": None,
			"atlas_tls_private_key": None,
		}
		settings = SimpleNamespace(
			wildcard_domain="example.test",
			get_password=Mock(side_effect=lambda field, **_kwargs: stored[field]),
		)
		ensure_atlas_client_certificate(settings)
		stored["atlas_tls_certificate"] = settings.atlas_tls_certificate
		stored["atlas_tls_private_key"] = settings.atlas_tls_private_key

		self.assertFalse(ensure_atlas_client_certificate(settings))

	def test_client_certificate_check_runs_once_per_settings_version(self) -> None:
		_client_certificate_files.clear_cache()
		settings = SimpleNamespace(modified="2026-09-26 10:00:00", get_password=Mock(return_value="pem"))
		with (
			patch("atlas.atlas.core.tls.metal.frappe.get_cached_doc", return_value=settings),
			patch("atlas.atlas.core.tls.metal.frappe.get_single", return_value=settings),
			patch("atlas.atlas.core.tls.metal.ensure_atlas_client_certificate", return_value=False) as ensure,
			patch("atlas.atlas.core.tls.metal._write_private_file", side_effect=lambda name, _content: name),
		):
			self.assertEqual(client_certificate_files(), ("atlas.crt", "atlas.key"))
			client_certificate_files()
			self.assertEqual(ensure.call_count, 1)

			settings.modified = "2026-09-26 11:00:00"
			client_certificate_files()
			self.assertEqual(ensure.call_count, 2)
		_client_certificate_files.clear_cache()
