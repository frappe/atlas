from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv6Address

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

CERTIFICATE_KEY_SIZE = 2048
CA_VALIDITY_DAYS = 3_650
CERTIFICATE_VALIDITY_DAYS = 825


class CertificateError(Exception):
	"""A certificate or private key is invalid."""


@dataclass(frozen=True)
class CertificateDetails:
	"""Issued certificate details."""

	expires_on: datetime
	dns_names: tuple[str, ...]
	ip_addresses: tuple[IPv4Address | IPv6Address, ...]


def read_certificate(certificate_pem: str) -> CertificateDetails:
	"""Return the leaf certificate expiry and subject alternative names."""
	leaf = _load_leaf(certificate_pem)
	try:
		alternative_names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
	except x509.ExtensionNotFound as error:
		raise CertificateError("The certificate carries no subject alternative name.") from error

	return CertificateDetails(
		leaf.not_valid_after_utc,
		tuple(alternative_names.value.get_values_for_type(x509.DNSName)),
		tuple(alternative_names.value.get_values_for_type(x509.IPAddress)),
	)


def read_expiry(certificate_pem: str) -> datetime:
	"""Return the leaf certificate expiry. A certificate authority carries no subject alternative name."""
	return _load_leaf(certificate_pem).not_valid_after_utc


def create_certificate_authority(identity: str) -> tuple[str, str]:
	"""Create a regional certificate authority."""
	private_key = create_private_key()
	now = datetime.now(UTC)
	subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, identity)])
	certificate = (
		x509.CertificateBuilder()
		.subject_name(subject)
		.issuer_name(subject)
		.public_key(private_key.public_key())
		.serial_number(x509.random_serial_number())
		.not_valid_before(now - timedelta(minutes=5))
		.not_valid_after(now + timedelta(days=CA_VALIDITY_DAYS))
		.add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
		.add_extension(
			x509.KeyUsage(
				digital_signature=True,
				content_commitment=False,
				key_encipherment=False,
				data_encipherment=False,
				key_agreement=False,
				key_cert_sign=True,
				crl_sign=True,
				encipher_only=False,
				decipher_only=False,
			),
			critical=True,
		)
		.add_extension(x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()), critical=False)
		.sign(private_key, hashes.SHA256())
	)
	return (
		certificate.public_bytes(serialization.Encoding.PEM).decode(),
		serialize_private_key(private_key),
	)


def issue_certificate(
	ca_certificate_pem: str,
	ca_private_key_pem: str,
	identity: str,
	ip_addresses: list[IPv4Address | IPv6Address],
) -> tuple[str, str]:
	"""Issue one Metal client and server certificate."""
	ca_certificate = _load_leaf(ca_certificate_pem)
	try:
		ca_private_key = serialization.load_pem_private_key(ca_private_key_pem.encode(), password=None)
	except (ValueError, TypeError) as error:
		raise CertificateError("The certificate authority private key is not valid PEM.") from error
	if _public_bytes(ca_private_key.public_key()) != _public_bytes(ca_certificate.public_key()):
		raise CertificateError("The certificate authority key does not match its certificate.")
	_validate_certificate_authority(ca_certificate)

	private_key = create_private_key()
	now = datetime.now(UTC)
	alternative_names: list[x509.GeneralName] = [x509.DNSName(identity)]
	alternative_names.extend(x509.IPAddress(address) for address in ip_addresses)
	certificate = (
		x509.CertificateBuilder()
		.subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, identity)]))
		.issuer_name(ca_certificate.subject)
		.public_key(private_key.public_key())
		.serial_number(x509.random_serial_number())
		.not_valid_before(now - timedelta(minutes=5))
		.not_valid_after(
			min(now + timedelta(days=CERTIFICATE_VALIDITY_DAYS), ca_certificate.not_valid_after_utc)
		)
		.add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
		.add_extension(
			x509.KeyUsage(
				digital_signature=True,
				content_commitment=False,
				key_encipherment=False,
				data_encipherment=False,
				key_agreement=False,
				key_cert_sign=False,
				crl_sign=False,
				encipher_only=False,
				decipher_only=False,
			),
			critical=True,
		)
		.add_extension(x509.SubjectAlternativeName(alternative_names), critical=False)
		.add_extension(
			x509.ExtendedKeyUsage(
				[x509.oid.ExtendedKeyUsageOID.SERVER_AUTH, x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]
			),
			critical=False,
		)
		.add_extension(x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()), critical=False)
		# OpenSSL 3.5 requires an authority key identifier on issued certificates.
		.add_extension(
			x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_certificate.public_key()),
			critical=False,
		)
		.sign(ca_private_key, hashes.SHA256())
	)
	return (
		certificate.public_bytes(serialization.Encoding.PEM).decode(),
		serialize_private_key(private_key),
	)


def verify_issued_certificate(certificate_pem: str, ca_certificate_pem: str) -> None:
	"""Verify that a current certificate was issued by the regional CA."""
	certificate = _load_leaf(certificate_pem)
	ca_certificate = _load_leaf(ca_certificate_pem)
	_validate_certificate_authority(ca_certificate)
	try:
		certificate.verify_directly_issued_by(ca_certificate)
	except (ValueError, TypeError, InvalidSignature) as error:
		raise CertificateError(
			"The certificate was not issued by the regional certificate authority."
		) from error
	if certificate.not_valid_before_utc > datetime.now(
		UTC
	) or certificate.not_valid_after_utc <= datetime.now(UTC):
		raise CertificateError("The certificate is not currently valid.")


def verify_key_pair(certificate_pem: str, private_key_pem: str) -> None:
	"""Raise an error when the key does not match the leaf certificate."""
	leaf = _load_leaf(certificate_pem)
	try:
		private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
	except (ValueError, TypeError) as error:
		raise CertificateError("The private key is not valid PEM.") from error

	if _public_bytes(private_key.public_key()) != _public_bytes(leaf.public_key()):
		raise CertificateError("The private key does not match the certificate.")


def create_private_key() -> rsa.RSAPrivateKey:
	"""Create a certificate private key."""
	return rsa.generate_private_key(public_exponent=65537, key_size=CERTIFICATE_KEY_SIZE)


def serialize_private_key(private_key: rsa.RSAPrivateKey) -> str:
	"""Return an unencrypted PKCS#8 PEM private key."""
	return private_key.private_bytes(
		encoding=serialization.Encoding.PEM,
		format=serialization.PrivateFormat.PKCS8,
		encryption_algorithm=serialization.NoEncryption(),
	).decode()


def create_certificate_request(private_key: rsa.RSAPrivateKey, dns_names: list[str]) -> bytes:
	"""Return a DER certificate request for the DNS names."""
	subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0])])
	request = (
		x509.CertificateSigningRequestBuilder()
		.subject_name(subject)
		.add_extension(
			x509.SubjectAlternativeName([x509.DNSName(name) for name in dns_names]), critical=False
		)
		.sign(private_key, hashes.SHA256())
	)
	return request.public_bytes(serialization.Encoding.DER)


def _public_bytes(public_key) -> bytes:
	return public_key.public_bytes(
		encoding=serialization.Encoding.DER,
		format=serialization.PublicFormat.SubjectPublicKeyInfo,
	)


def _load_leaf(certificate_pem: str) -> x509.Certificate:
	try:
		certificates = x509.load_pem_x509_certificates(certificate_pem.encode())
	except ValueError as error:
		raise CertificateError("The certificate is not valid PEM.") from error

	if not certificates:
		raise CertificateError("The certificate chain is empty.")
	return certificates[0]


def _validate_certificate_authority(certificate: x509.Certificate) -> None:
	try:
		constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
	except x509.ExtensionNotFound as error:
		raise CertificateError("The certificate authority has no basic constraints.") from error
	if not constraints.ca:
		raise CertificateError("The certificate is not a certificate authority.")
	if certificate.not_valid_before_utc > datetime.now(
		UTC
	) or certificate.not_valid_after_utc <= datetime.now(UTC):
		raise CertificateError("The certificate authority is not currently valid.")
