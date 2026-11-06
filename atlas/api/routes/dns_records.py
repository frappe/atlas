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
	request_example={"address": "fdaa:1:0:0:0:0:0:7"},
	responses={
		204: {"description": "The name points to the address. Repeating the request is safe."},
		400: {"description": "The name is not allowed, or the address does not suit the name."},
		**DNS_PROVIDER_FAILED,
	},
)
def set_dns_record(name: str, payload: DnsRecordPayload) -> ApiResult[None]:
	"""Set DNS record.

	Points a regional service name, such as `s3-svc.<domain>`, to a public address, or its `internal-` name, such as `internal-s3-svc.<domain>`, to its WG Mesh address. The TTL defaults to 300 seconds.
	"""
	require_tenant_zero_operator()
	try:
		get_dns_provider().upsert_service_record(name, payload.address, payload.ttl)
	except DnsProviderError as error:
		raise ApiError(str(error), code="dns_provider_error", status=502) from error
	return ApiResult(None, status=204)


@dns_records.delete("<name>")
@api_docs(
	responses={
		204: {"description": "The name has no A or AAAA record. Removing twice is safe."},
		400: {"description": "The name is not allowed."},
		**DNS_PROVIDER_FAILED,
	},
)
def remove_dns_record(name: str) -> ApiResult[None]:
	"""Remove DNS record.

	Removes the A and AAAA records of one regional service name.
	"""
	require_tenant_zero_operator()
	try:
		get_dns_provider().remove_service_record(name)
	except DnsProviderError as error:
		raise ApiError(str(error), code="dns_provider_error", status=502) from error
	return ApiResult(None, status=204)
