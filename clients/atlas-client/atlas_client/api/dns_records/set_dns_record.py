from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.api_error_response import ApiErrorResponse
from ...models.dns_record_payload import DnsRecordPayload
from typing import cast



def _get_kwargs(
    name: str,
    *,
    body: DnsRecordPayload,
    x_tenant_id: int,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    headers["X-Tenant-ID"] = str(x_tenant_id)




    

    

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/api/atlas/dns-records/{name}".format(name=quote(str(name), safe=""),),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Any | ApiErrorResponse | None:
    if response.status_code == 204:
        response_204 = cast(Any, None)
        return response_204

    if response.status_code == 400:
        response_400 = ApiErrorResponse.from_dict(response.json())



        return response_400

    if response.status_code == 401:
        response_401 = ApiErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 403:
        response_403 = ApiErrorResponse.from_dict(response.json())



        return response_403

    if response.status_code == 404:
        response_404 = ApiErrorResponse.from_dict(response.json())



        return response_404

    if response.status_code == 500:
        response_500 = ApiErrorResponse.from_dict(response.json())



        return response_500

    if response.status_code == 502:
        response_502 = ApiErrorResponse.from_dict(response.json())



        return response_502

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[Any | ApiErrorResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    name: str,
    *,
    client: AuthenticatedClient | Client,
    body: DnsRecordPayload,
    x_tenant_id: int,

) -> Response[Any | ApiErrorResponse]:
    """ Set DNS record

     Points a regional service name, such as `s3-svc.<domain>`, to a public address, or its `internal-`
    name, such as `internal-s3-svc.<domain>`, to its WG Mesh address. The TTL defaults to 300 seconds.

    Args:
        name (str):
        x_tenant_id (int):
        body (DnsRecordPayload): The address of one regional service name.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ApiErrorResponse]
     """


    kwargs = _get_kwargs(
        name=name,
body=body,
x_tenant_id=x_tenant_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    name: str,
    *,
    client: AuthenticatedClient | Client,
    body: DnsRecordPayload,
    x_tenant_id: int,

) -> Any | ApiErrorResponse | None:
    """ Set DNS record

     Points a regional service name, such as `s3-svc.<domain>`, to a public address, or its `internal-`
    name, such as `internal-s3-svc.<domain>`, to its WG Mesh address. The TTL defaults to 300 seconds.

    Args:
        name (str):
        x_tenant_id (int):
        body (DnsRecordPayload): The address of one regional service name.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ApiErrorResponse
     """


    return sync_detailed(
        name=name,
client=client,
body=body,
x_tenant_id=x_tenant_id,

    ).parsed

async def asyncio_detailed(
    name: str,
    *,
    client: AuthenticatedClient | Client,
    body: DnsRecordPayload,
    x_tenant_id: int,

) -> Response[Any | ApiErrorResponse]:
    """ Set DNS record

     Points a regional service name, such as `s3-svc.<domain>`, to a public address, or its `internal-`
    name, such as `internal-s3-svc.<domain>`, to its WG Mesh address. The TTL defaults to 300 seconds.

    Args:
        name (str):
        x_tenant_id (int):
        body (DnsRecordPayload): The address of one regional service name.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ApiErrorResponse]
     """


    kwargs = _get_kwargs(
        name=name,
body=body,
x_tenant_id=x_tenant_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    name: str,
    *,
    client: AuthenticatedClient | Client,
    body: DnsRecordPayload,
    x_tenant_id: int,

) -> Any | ApiErrorResponse | None:
    """ Set DNS record

     Points a regional service name, such as `s3-svc.<domain>`, to a public address, or its `internal-`
    name, such as `internal-s3-svc.<domain>`, to its WG Mesh address. The TTL defaults to 300 seconds.

    Args:
        name (str):
        x_tenant_id (int):
        body (DnsRecordPayload): The address of one regional service name.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ApiErrorResponse
     """


    return (await asyncio_detailed(
        name=name,
client=client,
body=body,
x_tenant_id=x_tenant_id,

    )).parsed
