from __future__ import annotations

from atlas.api.core.base import ApiResult
from atlas.api.core.docs import api_docs
from atlas.api.core.errors import ApiError
from atlas.api.models import DnsRecordPayload
from atlas.api.router import dns_records
from atlas.atlas.core.dns_providers import DnsProviderError, get_dns_provider
from atlas.auth.identity import require_tenant_zero_operator

DNS_PROVIDER_FAILED = {
	502: {"description": "The DNS provider rejected the change. The message is its error."}
}


@dns_records.put("<name>")
@api_docs(
	request_example={
		"addresses": ["203.0.113.7", "203.0.113.8"],
		"health_check_url": "https://s3-admin-svc.example.com/health",
	},
	responses={
		204: {"description": "The name holds the record. Repeating the request is safe."},
		400: {"description": "The name is not allowed, or the record does not suit the name."},
		**DNS_PROVIDER_FAILED,
	},
)
def set_dns_record(name: str, payload: DnsRecordPayload) -> ApiResult[None]:
	"""Set DNS record.

	Points a regional service name, such as `s3-svc.<domain>`, to public addresses, or its `internal-` name, such as `internal-s3-svc.<domain>`, to WG Mesh addresses. The addresses replace the ones the name had. With `health_check_url`, each IPv4 address gets a Route53 health check, and DNS answers with the addresses that pass. With `type` TXT or CNAME, sets `values` on a service name or a name under it, such as `_acme-challenge.s3-svc.<domain>`. The TTL defaults to 300 seconds.
	"""
	require_tenant_zero_operator()
	provider = get_dns_provider()
	try:
		if payload.type in ("TXT", "CNAME"):
			provider.upsert_service_values(payload.type, name, payload.values, payload.ttl)
		else:
			health_check_url = str(payload.health_check_url) if payload.health_check_url else None
			provider.set_service_addresses(name, payload.service_addresses, payload.ttl, health_check_url)
	except DnsProviderError as error:
		raise ApiError(str(error), code="dns_provider_error", status=502) from error
	return ApiResult(None, status=204)


@dns_records.delete("<name>")
@api_docs(
	responses={
		204: {"description": "The name has no record left. Removing twice is safe."},
		400: {"description": "The name is not allowed."},
		**DNS_PROVIDER_FAILED,
	},
)
def remove_dns_record(name: str) -> ApiResult[None]:
	"""Remove DNS record.

	Removes the A, AAAA, TXT and CNAME records of one regional service name, or of a name under it.
	"""
	require_tenant_zero_operator()
	try:
		get_dns_provider().remove_service_record(name)
	except DnsProviderError as error:
		raise ApiError(str(error), code="dns_provider_error", status=502) from error
	return ApiResult(None, status=204)
