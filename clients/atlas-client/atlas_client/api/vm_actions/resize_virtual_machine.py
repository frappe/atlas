from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.capacity_unavailable_response import CapacityUnavailableResponse
from ...models.resize_payload import ResizePayload
from ...models.virtual_machine_response import VirtualMachineResponse
from typing import cast



def _get_kwargs(
    virtual_machine_id: str,
    *,
    body: ResizePayload,
    x_tenant_id: int,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    headers["X-Tenant-ID"] = str(x_tenant_id)




    

    

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/atlas/virtual-machines/{virtual_machine_id}/actions/resize".format(virtual_machine_id=quote(str(virtual_machine_id), safe=""),),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> CapacityUnavailableResponse | VirtualMachineResponse | None:
    if response.status_code == 202:
        response_202 = VirtualMachineResponse.from_dict(response.json())



        return response_202

    if response.status_code == 503:
        response_503 = CapacityUnavailableResponse.from_dict(response.json())



        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[CapacityUnavailableResponse | VirtualMachineResponse]:
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
    body: ResizePayload,
    x_tenant_id: int,

) -> Response[CapacityUnavailableResponse | VirtualMachineResponse]:
    """ Resize VM

     Changes CPU, memory, disk, or idle shutdown. Omitted fields keep their current value. The disk only
    grows.

    Stop the VM before changing CPU, memory, or disk. Atlas resizes in place or migrates it. Idle-only
    changes work in any VM state. `0` disables idle shutdown.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (ResizePayload): VM resource and idle shutdown changes.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CapacityUnavailableResponse | VirtualMachineResponse]
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
    body: ResizePayload,
    x_tenant_id: int,

) -> CapacityUnavailableResponse | VirtualMachineResponse | None:
    """ Resize VM

     Changes CPU, memory, disk, or idle shutdown. Omitted fields keep their current value. The disk only
    grows.

    Stop the VM before changing CPU, memory, or disk. Atlas resizes in place or migrates it. Idle-only
    changes work in any VM state. `0` disables idle shutdown.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (ResizePayload): VM resource and idle shutdown changes.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CapacityUnavailableResponse | VirtualMachineResponse
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
    body: ResizePayload,
    x_tenant_id: int,

) -> Response[CapacityUnavailableResponse | VirtualMachineResponse]:
    """ Resize VM

     Changes CPU, memory, disk, or idle shutdown. Omitted fields keep their current value. The disk only
    grows.

    Stop the VM before changing CPU, memory, or disk. Atlas resizes in place or migrates it. Idle-only
    changes work in any VM state. `0` disables idle shutdown.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (ResizePayload): VM resource and idle shutdown changes.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CapacityUnavailableResponse | VirtualMachineResponse]
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
    body: ResizePayload,
    x_tenant_id: int,

) -> CapacityUnavailableResponse | VirtualMachineResponse | None:
    """ Resize VM

     Changes CPU, memory, disk, or idle shutdown. Omitted fields keep their current value. The disk only
    grows.

    Stop the VM before changing CPU, memory, or disk. Atlas resizes in place or migrates it. Idle-only
    changes work in any VM state. `0` disables idle shutdown.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (ResizePayload): VM resource and idle shutdown changes.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CapacityUnavailableResponse | VirtualMachineResponse
     """


    return (await asyncio_detailed(
        virtual_machine_id=virtual_machine_id,
client=client,
body=body,
x_tenant_id=x_tenant_id,

    )).parsed
