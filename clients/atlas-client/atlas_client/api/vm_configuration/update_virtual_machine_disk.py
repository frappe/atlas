from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.disk_update_payload import DiskUpdatePayload
from ...models.virtual_machine_response import VirtualMachineResponse
from typing import cast



def _get_kwargs(
    virtual_machine_id: str,
    *,
    body: DiskUpdatePayload,
    x_tenant_id: int,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    headers["X-Tenant-ID"] = str(x_tenant_id)




    

    

    _kwargs: dict[str, Any] = {
        "method": "patch",
        "url": "/api/atlas/virtual-machines/{virtual_machine_id}/disk".format(virtual_machine_id=quote(str(virtual_machine_id), safe=""),),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Any | VirtualMachineResponse | None:
    if response.status_code == 202:
        response_202 = VirtualMachineResponse.from_dict(response.json())



        return response_202

    if response.status_code == 409:
        response_409 = cast(Any, None)
        return response_409

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[Any | VirtualMachineResponse]:
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
    body: DiskUpdatePayload,
    x_tenant_id: int,

) -> Response[Any | VirtualMachineResponse]:
    """ Update disk in-place

     Works while the VM runs. The disk only grows, and `0` removes a limit. A full host returns `409
    insufficient_capacity`. Stop the VM and use resize to move it.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (DiskUpdatePayload): New disk size and disk rate limits.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | VirtualMachineResponse]
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
    body: DiskUpdatePayload,
    x_tenant_id: int,

) -> Any | VirtualMachineResponse | None:
    """ Update disk in-place

     Works while the VM runs. The disk only grows, and `0` removes a limit. A full host returns `409
    insufficient_capacity`. Stop the VM and use resize to move it.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (DiskUpdatePayload): New disk size and disk rate limits.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | VirtualMachineResponse
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
    body: DiskUpdatePayload,
    x_tenant_id: int,

) -> Response[Any | VirtualMachineResponse]:
    """ Update disk in-place

     Works while the VM runs. The disk only grows, and `0` removes a limit. A full host returns `409
    insufficient_capacity`. Stop the VM and use resize to move it.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (DiskUpdatePayload): New disk size and disk rate limits.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | VirtualMachineResponse]
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
    body: DiskUpdatePayload,
    x_tenant_id: int,

) -> Any | VirtualMachineResponse | None:
    """ Update disk in-place

     Works while the VM runs. The disk only grows, and `0` removes a limit. A full host returns `409
    insufficient_capacity`. Stop the VM and use resize to move it.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (DiskUpdatePayload): New disk size and disk rate limits.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | VirtualMachineResponse
     """


    return (await asyncio_detailed(
        virtual_machine_id=virtual_machine_id,
client=client,
body=body,
x_tenant_id=x_tenant_id,

    )).parsed
