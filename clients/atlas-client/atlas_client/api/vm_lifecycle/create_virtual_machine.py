from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.api_error_response import ApiErrorResponse
from ...models.capacity_unavailable_response import CapacityUnavailableResponse
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



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> ApiErrorResponse | CapacityUnavailableResponse | VirtualMachineResponse | None:
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

    if response.status_code == 500:
        response_500 = ApiErrorResponse.from_dict(response.json())



        return response_500

    if response.status_code == 503:
        response_503 = CapacityUnavailableResponse.from_dict(response.json())



        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[ApiErrorResponse | CapacityUnavailableResponse | VirtualMachineResponse]:
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

) -> Response[ApiErrorResponse | CapacityUnavailableResponse | VirtualMachineResponse]:
    """ Create VM

     Creates a tenant VM from an image and requests the specified compute, disk, network, and guest
    configuration. Only tenant 0 can set `is_privileged`, which lets the VM reach every tenant through
    the mesh.

    Set `is_termination_protected` to refuse deletion of the new VM. The termination protection route
    changes it later.

    Use `tags` to label the VM, for example, `{"role": "cargo-server"}`. `placement_rules` limits the
    Metal Servers for the VM by host tags and by the tags of other VMs on the host. Atlas Settings
    selects whether a VM that no host with room can satisfy fails with `affinity_unsatisfied` or goes to
    any host.

    Args:
        x_tenant_id (int):
        body (CreateVirtualMachinePayload): Values that create one virtual machine.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApiErrorResponse | CapacityUnavailableResponse | VirtualMachineResponse]
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

) -> ApiErrorResponse | CapacityUnavailableResponse | VirtualMachineResponse | None:
    """ Create VM

     Creates a tenant VM from an image and requests the specified compute, disk, network, and guest
    configuration. Only tenant 0 can set `is_privileged`, which lets the VM reach every tenant through
    the mesh.

    Set `is_termination_protected` to refuse deletion of the new VM. The termination protection route
    changes it later.

    Use `tags` to label the VM, for example, `{"role": "cargo-server"}`. `placement_rules` limits the
    Metal Servers for the VM by host tags and by the tags of other VMs on the host. Atlas Settings
    selects whether a VM that no host with room can satisfy fails with `affinity_unsatisfied` or goes to
    any host.

    Args:
        x_tenant_id (int):
        body (CreateVirtualMachinePayload): Values that create one virtual machine.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApiErrorResponse | CapacityUnavailableResponse | VirtualMachineResponse
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

) -> Response[ApiErrorResponse | CapacityUnavailableResponse | VirtualMachineResponse]:
    """ Create VM

     Creates a tenant VM from an image and requests the specified compute, disk, network, and guest
    configuration. Only tenant 0 can set `is_privileged`, which lets the VM reach every tenant through
    the mesh.

    Set `is_termination_protected` to refuse deletion of the new VM. The termination protection route
    changes it later.

    Use `tags` to label the VM, for example, `{"role": "cargo-server"}`. `placement_rules` limits the
    Metal Servers for the VM by host tags and by the tags of other VMs on the host. Atlas Settings
    selects whether a VM that no host with room can satisfy fails with `affinity_unsatisfied` or goes to
    any host.

    Args:
        x_tenant_id (int):
        body (CreateVirtualMachinePayload): Values that create one virtual machine.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApiErrorResponse | CapacityUnavailableResponse | VirtualMachineResponse]
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

) -> ApiErrorResponse | CapacityUnavailableResponse | VirtualMachineResponse | None:
    """ Create VM

     Creates a tenant VM from an image and requests the specified compute, disk, network, and guest
    configuration. Only tenant 0 can set `is_privileged`, which lets the VM reach every tenant through
    the mesh.

    Set `is_termination_protected` to refuse deletion of the new VM. The termination protection route
    changes it later.

    Use `tags` to label the VM, for example, `{"role": "cargo-server"}`. `placement_rules` limits the
    Metal Servers for the VM by host tags and by the tags of other VMs on the host. Atlas Settings
    selects whether a VM that no host with room can satisfy fails with `affinity_unsatisfied` or goes to
    any host.

    Args:
        x_tenant_id (int):
        body (CreateVirtualMachinePayload): Values that create one virtual machine.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApiErrorResponse | CapacityUnavailableResponse | VirtualMachineResponse
     """


    return (await asyncio_detailed(
        client=client,
body=body,
x_tenant_id=x_tenant_id,

    )).parsed
