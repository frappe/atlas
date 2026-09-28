from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.api_error_response import ApiErrorResponse
from ...models.virtual_machine_metrics_response import VirtualMachineMetricsResponse
from typing import cast



def _get_kwargs(
    virtual_machine_id: str,
    *,
    x_tenant_id: int,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    headers["X-Tenant-ID"] = str(x_tenant_id)




    

    

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/atlas/virtual-machines/{virtual_machine_id}/metrics".format(virtual_machine_id=quote(str(virtual_machine_id), safe=""),),
    }


    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> ApiErrorResponse | VirtualMachineMetricsResponse | None:
    if response.status_code == 200:
        response_200 = VirtualMachineMetricsResponse.from_dict(response.json())



        return response_200

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


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[ApiErrorResponse | VirtualMachineMetricsResponse]:
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
    x_tenant_id: int,

) -> Response[ApiErrorResponse | VirtualMachineMetricsResponse]:
    """ Get VM metrics

     CPU time belongs to the current guest process. Memory is charged to the Firecracker cgroup. Disk use
    is from the last reconcile pass. Network counters last for the traffic attachment's lifetime,
    including guest stops. A stopped guest reports zero CPU and memory.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApiErrorResponse | VirtualMachineMetricsResponse]
     """


    kwargs = _get_kwargs(
        virtual_machine_id=virtual_machine_id,
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
    x_tenant_id: int,

) -> ApiErrorResponse | VirtualMachineMetricsResponse | None:
    """ Get VM metrics

     CPU time belongs to the current guest process. Memory is charged to the Firecracker cgroup. Disk use
    is from the last reconcile pass. Network counters last for the traffic attachment's lifetime,
    including guest stops. A stopped guest reports zero CPU and memory.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApiErrorResponse | VirtualMachineMetricsResponse
     """


    return sync_detailed(
        virtual_machine_id=virtual_machine_id,
client=client,
x_tenant_id=x_tenant_id,

    ).parsed

async def asyncio_detailed(
    virtual_machine_id: str,
    *,
    client: AuthenticatedClient | Client,
    x_tenant_id: int,

) -> Response[ApiErrorResponse | VirtualMachineMetricsResponse]:
    """ Get VM metrics

     CPU time belongs to the current guest process. Memory is charged to the Firecracker cgroup. Disk use
    is from the last reconcile pass. Network counters last for the traffic attachment's lifetime,
    including guest stops. A stopped guest reports zero CPU and memory.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApiErrorResponse | VirtualMachineMetricsResponse]
     """


    kwargs = _get_kwargs(
        virtual_machine_id=virtual_machine_id,
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
    x_tenant_id: int,

) -> ApiErrorResponse | VirtualMachineMetricsResponse | None:
    """ Get VM metrics

     CPU time belongs to the current guest process. Memory is charged to the Firecracker cgroup. Disk use
    is from the last reconcile pass. Network counters last for the traffic attachment's lifetime,
    including guest stops. A stopped guest reports zero CPU and memory.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApiErrorResponse | VirtualMachineMetricsResponse
     """


    return (await asyncio_detailed(
        virtual_machine_id=virtual_machine_id,
client=client,
x_tenant_id=x_tenant_id,

    )).parsed
