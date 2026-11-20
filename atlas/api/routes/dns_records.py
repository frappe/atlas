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
		"type": "A",
		"values": ["203.0.113.7", "203.0.113.8"],
		"health_check": {"host": "s3-admin-svc.example.com", "path": "/health"},
	},
	responses={
		204: {"description": "The name holds the records. Repeating the request is safe."},
		400: {"description": "The name is not allowed, or the values do not suit the name or the type."},
		**DNS_PROVIDER_FAILED,
	},
)
def set_dns_record(name: str, payload: DnsRecordPayload) -> ApiResult[None]:
	"""Set DNS record.

	Sets the A, AAAA, TXT or CNAME records of a regional service name, such as `s3-svc.<domain>`, its `internal-` name, or one name under either, such as `_acme-challenge.s3-svc.<domain>`. The values replace the values the name had.
	"""
	require_tenant_zero_operator()
	try:
		get_dns_provider().upsert_dns_records(
			name, payload.type, payload.values, payload.ttl, payload.health_check
		)
	except DnsProviderError as error:
		raise ApiError(str(error), code="dns_provider_error", status=502) from error
	return ApiResult(None, status=204)


@dns_records.delete("<name>")
@api_docs(
	responses={
		204: {"description": "The name has no records left. Removing twice is safe."},
		400: {"description": "The name is not allowed."},
		**DNS_PROVIDER_FAILED,
	},
)
def remove_dns_record(name: str) -> ApiResult[None]:
	"""Remove DNS record.

	Removes every record of a regional service name, or of one name under it, and its health checks.
	"""
	require_tenant_zero_operator()
	try:
		get_dns_provider().remove_dns_records(name)
	except DnsProviderError as error:
		raise ApiError(str(error), code="dns_provider_error", status=502) from error
	return ApiResult(None, status=204)
