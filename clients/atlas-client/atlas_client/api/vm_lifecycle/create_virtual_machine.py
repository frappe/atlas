from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.create_virtual_machine_payload import CreateVirtualMachinePayload
from ...models.virtual_machine_response import VirtualMachineResponse
from typing import cast



def _get_kwargs(
    *,
    body: CreateVirtualMachinePayload,
    x_tenant_id: int,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    headers["X-Tenant-ID"] = str(x_tenant_id)




    

    

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/atlas/virtual-machines",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> VirtualMachineResponse | None:
    if response.status_code == 201:
        response_201 = VirtualMachineResponse.from_dict(response.json())



        return response_201

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[VirtualMachineResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CreateVirtualMachinePayload,
    x_tenant_id: int,

) -> Response[VirtualMachineResponse]:
    """ Create VM

     Creates a tenant VM from an image and requests the specified compute, disk, network, and guest
    configuration. Only tenant 0 can set `is_privileged`, which lets the VM reach every tenant through
    the mesh.

    Args:
        x_tenant_id (int):
        body (CreateVirtualMachinePayload): Values that create one virtual machine.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[VirtualMachineResponse]
     """


    kwargs = _get_kwargs(
        body=body,
x_tenant_id=x_tenant_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient | Client,
    body: CreateVirtualMachinePayload,
    x_tenant_id: int,

) -> VirtualMachineResponse | None:
    """ Create VM

     Creates a tenant VM from an image and requests the specified compute, disk, network, and guest
    configuration. Only tenant 0 can set `is_privileged`, which lets the VM reach every tenant through
    the mesh.

    Args:
        x_tenant_id (int):
        body (CreateVirtualMachinePayload): Values that create one virtual machine.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        VirtualMachineResponse
     """


    return sync_detailed(
        client=client,
body=body,
x_tenant_id=x_tenant_id,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CreateVirtualMachinePayload,
    x_tenant_id: int,

) -> Response[VirtualMachineResponse]:
    """ Create VM

     Creates a tenant VM from an image and requests the specified compute, disk, network, and guest
    configuration. Only tenant 0 can set `is_privileged`, which lets the VM reach every tenant through
    the mesh.

    Args:
        x_tenant_id (int):
        body (CreateVirtualMachinePayload): Values that create one virtual machine.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[VirtualMachineResponse]
     """


    kwargs = _get_kwargs(
        body=body,
x_tenant_id=x_tenant_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CreateVirtualMachinePayload,
    x_tenant_id: int,

) -> VirtualMachineResponse | None:
    """ Create VM

     Creates a tenant VM from an image and requests the specified compute, disk, network, and guest
    configuration. Only tenant 0 can set `is_privileged`, which lets the VM reach every tenant through
    the mesh.

    Args:
        x_tenant_id (int):
        body (CreateVirtualMachinePayload): Values that create one virtual machine.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        VirtualMachineResponse
     """


    return (await asyncio_detailed(
        client=client,
body=body,
x_tenant_id=x_tenant_id,

    )).parsed
