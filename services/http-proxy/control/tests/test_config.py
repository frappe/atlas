from pathlib import Path

import pytest

from proxy_control.config import (
	DEFAULT_ADMIN_SOCKET,
	ConfigError,
	load,
)

FULL = """
[control]
domain = "proxy.par-1.example.com"
node_domain = "proxy-001.par-1.example.com"
admin_socket = "/run/nginx/other.sock"
cert_dir = "/srv/certs"

[auto_proxy]
address_prefix = "fdaa:1"
host_prefixes = ["site-", "*-vm-"]

[auth]
password_hash = "current-hash"
previous_password_hash = "previous-hash"
previous_password_valid_until = 1788800000
jwks_url = "https://issuer.example.com/jwks.json"
jwks_audience_id = "atlas-proxy:42"
jwks_issuers = ["central", "atlas:42"]

[cluster]
node_id = "proxy-001"
password = "current-secret"
previous_password = "previous-secret"
previous_password_valid_until = 1788800000
peers = [
  { node_id = "proxy-001", address = "https://proxy-001.par-1.example.com" },
  { node_id = "proxy-002", address = "https://proxy-002.par-1.example.com" },
]

[tls]
wildcard_domain = "*.par-1.example.com"
fullchain_pem = '''
-----BEGIN CERTIFICATE-----
leaf
-----END CERTIFICATE-----
'''
private_key_pem = '''
-----BEGIN PRIVATE KEY-----
key
-----END PRIVATE KEY-----
'''
"""


# An unconfigured proxy must not start.
def test_a_missing_file_is_refused(tmp_path: Path):
	with pytest.raises(ConfigError):
		load(tmp_path / "missing.toml")


def test_a_missing_tls_section_is_refused(tmp_path: Path):
	path = tmp_path / "proxy-control.toml"
	path.write_text('[auth]\njwks_url = "https://issuer.example.com/jwks.json"\n')

	with pytest.raises(ConfigError):
		load(path)


def test_a_full_file_is_read(tmp_path: Path):
	path = tmp_path / "proxy-control.toml"
	path.write_text(FULL)

	config = load(path)

	assert config.admin_socket == "/run/nginx/other.sock"
	assert config.cert_dir == Path("/srv/certs")
	assert config.auth.jwks_audience_id == "atlas-proxy:42"
	assert config.auth.jwks_issuers == ("central", "atlas:42")
	assert config.auth.password_hash == "current-hash"
	assert config.auth.previous_password_hash == "previous-hash"
	assert config.auth.previous_password_valid_until == 1788800000
	assert config.domain == "proxy.par-1.example.com"
	assert config.node_domain == "proxy-001.par-1.example.com"
	assert config.reserved_subdomains == ("proxy", "proxy-001")
	assert config.auto_proxy_address_prefix == "fdaa:1"
	assert config.auto_proxy_host_prefixes == ("site-", "*-vm-")
	assert config.cluster.node_id == "proxy-001"
	assert config.cluster.previous_password == "previous-secret"
	assert config.cluster.previous_password_valid_until == 1788800000
	assert len(config.cluster.peers) == 2
	assert config.tls is not None
	assert config.tls.wildcard_domain == "*.par-1.example.com"
	assert config.tls.fullchain_pem.startswith("-----BEGIN CERTIFICATE-----")
	assert config.tls.private_key_pem.endswith("-----END PRIVATE KEY-----")


def test_a_partial_file_keeps_the_defaults(tmp_path: Path):
	path = tmp_path / "proxy-control.toml"
	path.write_text(TLS_SECTION + '\n[auth]\njwks_url = "https://issuer.example.com/jwks.json"\n')

	config = load(path)

	assert config.auth.jwks_url == "https://issuer.example.com/jwks.json"
	assert config.auth.jwks_audience_id == ""
	assert config.auth.password_hash == ""
	assert config.auth.previous_password_valid_until == 0
	assert config.auto_proxy_address_prefix == ""
	assert config.auto_proxy_host_prefixes == ()
	assert config.tls.wildcard_domain == "*.par-1.example.com"


@pytest.mark.parametrize(
	"auth",
	[
		'jwks_audience_id = "atlas-proxy:42"\njwks_issuers = ["central", "atlas:43"]',
		'jwks_audience_id = "atlas-proxy:42"\njwks_issuers = ["central"]',
		'jwks_audience_id = "atlas-proxy:42"\njwks_issuers = ["central", "other"]',
	],
)
def test_jwks_issuer_and_audience_must_name_one_region(tmp_path: Path, auth: str):
	path = tmp_path / "proxy-control.toml"
	path.write_text(f"{TLS_SECTION}\n[auth]\n{auth}\n")

	with pytest.raises(ConfigError):
		load(path)


def test_malformed_toml_is_refused(tmp_path: Path):
	path = tmp_path / "proxy-control.toml"
	path.write_text("[control\n")

	with pytest.raises(ConfigError):
		load(path)


def test_a_configured_control_port_is_refused(tmp_path: Path):
	path = tmp_path / "proxy-control.toml"
	path.write_text("[control]\nport = 9200\n")

	with pytest.raises(ConfigError, match="control.port is fixed at 9000"):
		load(path)


# Half a certificate would install nothing and hide the mistake.
def test_an_incomplete_tls_section_is_refused(tmp_path: Path):
	path = tmp_path / "proxy-control.toml"
	path.write_text('[tls]\nwildcard_domain = "*.par-1.example.com"\n')

	with pytest.raises(ConfigError):
		load(path)


def test_a_tls_enabled_flag_is_refused(tmp_path: Path):
	path = tmp_path / "proxy-control.toml"
	path.write_text(TLS_SECTION + "\nenabled = false\n")

	with pytest.raises(ConfigError, match="tls.enabled is not supported"):
		load(path)


def test_the_config_path_environment_variable_wins(tmp_path: Path, monkeypatch):
	path = tmp_path / "elsewhere.toml"
	path.write_text(TLS_SECTION)
	monkeypatch.setenv("ATLAS_PROXY_CONTROL_CONFIG", str(path))

	assert load().admin_socket == DEFAULT_ADMIN_SOCKET


TLS_SECTION = """
[tls]
wildcard_domain = "*.par-1.example.com"
fullchain_pem = "leaf"
private_key_pem = "key"
"""


def _with_domain(tmp_path: Path, domain: str) -> Path:
	path = tmp_path / "proxy-control.toml"
	path.write_text(f'[control]\ndomain = "{domain}"\n{TLS_SECTION}')
	return path


# The first label is the reserved subdomain, so it needs no second setting.
def test_the_control_domain_gives_the_reserved_subdomain(tmp_path: Path):
	config = load(_with_domain(tmp_path, "proxy-001.par-1.example.com"))

	assert config.reserved_subdomain == "proxy-001"


def test_a_control_domain_outside_the_wildcard_is_refused(tmp_path: Path):
	with pytest.raises(ConfigError):
		load(_with_domain(tmp_path, "proxy-001.other.example.com"))


# The wildcard covers one label, so a deeper name has no certificate.
def test_a_control_domain_below_another_label_is_refused(tmp_path: Path):
	with pytest.raises(ConfigError):
		load(_with_domain(tmp_path, "a.proxy-001.par-1.example.com"))


def test_the_bare_zone_is_not_a_control_domain(tmp_path: Path):
	with pytest.raises(ConfigError):
		load(_with_domain(tmp_path, "par-1.example.com"))


def test_a_control_domain_without_tls_is_refused(tmp_path: Path):
	path = tmp_path / "proxy-control.toml"
	path.write_text('[control]\ndomain = "proxy-001.par-1.example.com"\n')

	with pytest.raises(ConfigError):
		load(path)


def test_no_control_domain_reserves_nothing(tmp_path: Path):
	path = tmp_path / "proxy-control.toml"
	path.write_text(TLS_SECTION)

	config = load(path)

	assert config.domain == ""
	assert config.reserved_subdomain == ""


def test_cluster_membership_must_include_the_local_node(tmp_path: Path):
	path = tmp_path / "proxy-control.toml"
	path.write_text(
		TLS_SECTION
		+ """
[cluster]
node_id = "proxy-001"
password = "secret"
peers = [{ node_id = "proxy-002", address = "https://proxy-002.par-1.example.com" }]
"""
	)

	with pytest.raises(ConfigError, match="must include"):
		load(path)
