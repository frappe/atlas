from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.api_error_response import ApiErrorResponse
from ...models.network_update_payload import NetworkUpdatePayload
from ...models.virtual_machine_response import VirtualMachineResponse
from typing import cast



def _get_kwargs(
    virtual_machine_id: str,
    *,
    body: NetworkUpdatePayload,
    x_tenant_id: int,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    headers["X-Tenant-ID"] = str(x_tenant_id)




    

    

    _kwargs: dict[str, Any] = {
        "method": "patch",
        "url": "/api/atlas/virtual-machines/{virtual_machine_id}/network".format(virtual_machine_id=quote(str(virtual_machine_id), safe=""),),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> ApiErrorResponse | VirtualMachineResponse | None:
    if response.status_code == 202:
        response_202 = VirtualMachineResponse.from_dict(response.json())



        return response_202

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

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[ApiErrorResponse | VirtualMachineResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    virtual_machine_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: NetworkUpdatePayload,
    x_tenant_id: int,

) -> Response[ApiErrorResponse | VirtualMachineResponse]:
    """ Update network

     Changes IPv4 internet access, WireGuard gateway access, network throughput limits, or firewall
    fields. `ipv4_internet_access` reaches the IPv4 internet through host NAT, and a public IPv4 address
    needs it. A public IPv6 address brings its own internet path.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (NetworkUpdatePayload): New IPv4 internet access, network rate limits, or firewall
            fields.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApiErrorResponse | VirtualMachineResponse]
     """


    kwargs = _get_kwargs(
        virtual_machine_id=virtual_machine_id,
body=body,
x_tenant_id=x_tenant_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    virtual_machine_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: NetworkUpdatePayload,
    x_tenant_id: int,

) -> ApiErrorResponse | VirtualMachineResponse | None:
    """ Update network

     Changes IPv4 internet access, WireGuard gateway access, network throughput limits, or firewall
    fields. `ipv4_internet_access` reaches the IPv4 internet through host NAT, and a public IPv4 address
    needs it. A public IPv6 address brings its own internet path.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (NetworkUpdatePayload): New IPv4 internet access, network rate limits, or firewall
            fields.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApiErrorResponse | VirtualMachineResponse
     """


    return sync_detailed(
        virtual_machine_id=virtual_machine_id,
client=client,
body=body,
x_tenant_id=x_tenant_id,

    ).parsed

async def asyncio_detailed(
    virtual_machine_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: NetworkUpdatePayload,
    x_tenant_id: int,

) -> Response[ApiErrorResponse | VirtualMachineResponse]:
    """ Update network

     Changes IPv4 internet access, WireGuard gateway access, network throughput limits, or firewall
    fields. `ipv4_internet_access` reaches the IPv4 internet through host NAT, and a public IPv4 address
    needs it. A public IPv6 address brings its own internet path.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (NetworkUpdatePayload): New IPv4 internet access, network rate limits, or firewall
            fields.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApiErrorResponse | VirtualMachineResponse]
     """


    kwargs = _get_kwargs(
        virtual_machine_id=virtual_machine_id,
body=body,
x_tenant_id=x_tenant_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    virtual_machine_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: NetworkUpdatePayload,
    x_tenant_id: int,

) -> ApiErrorResponse | VirtualMachineResponse | None:
    """ Update network

     Changes IPv4 internet access, WireGuard gateway access, network throughput limits, or firewall
    fields. `ipv4_internet_access` reaches the IPv4 internet through host NAT, and a public IPv4 address
    needs it. A public IPv6 address brings its own internet path.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (NetworkUpdatePayload): New IPv4 internet access, network rate limits, or firewall
            fields.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApiErrorResponse | VirtualMachineResponse
     """


    return (await asyncio_detailed(
        virtual_machine_id=virtual_machine_id,
client=client,
body=body,
x_tenant_id=x_tenant_id,

    )).parsed
