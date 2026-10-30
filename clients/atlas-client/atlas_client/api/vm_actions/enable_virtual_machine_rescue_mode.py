from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.api_error_response import ApiErrorResponse
from ...models.virtual_machine_response import VirtualMachineResponse
from typing import cast



def _get_kwargs(
    virtual_machine_id: str,
    *,
    x_tenant_id: int,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    headers["X-Tenant-ID"] = str(x_tenant_id)




    

    

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/atlas/virtual-machines/{virtual_machine_id}/actions/enable-rescue-mode".format(virtual_machine_id=quote(str(virtual_machine_id), safe=""),),
    }


    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> ApiErrorResponse | VirtualMachineResponse | None:
    if response.status_code == 202:
        response_202 = VirtualMachineResponse.from_dict(response.json())



        return response_202

    if response.status_code == 401:
        response_401 = ApiErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 403:
        response_403 = ApiErrorResponse.from_dict(response.json())



        return response_403

    if response.status_code == 404:
        response_404 = ApiErrorResponse.from_dict(response.json())



        return response_404

    if response.status_code == 409:
        response_409 = ApiErrorResponse.from_dict(response.json())



        return response_409

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
    x_tenant_id: int,

) -> Response[ApiErrorResponse | VirtualMachineResponse]:
    """ Enable rescue mode

     `is_rescue_mode_enabled` turns true now. The next restart boots the rescue image and attaches the VM
    disk as `/dev/vdb`. `is_in_rescue_mode` turns true after that boot. Rescue mode then blocks every
    change except a disk resize, a snapshot of the VM disk, a restart, power changes, and deletion.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApiErrorResponse | VirtualMachineResponse]
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

) -> ApiErrorResponse | VirtualMachineResponse | None:
    """ Enable rescue mode

     `is_rescue_mode_enabled` turns true now. The next restart boots the rescue image and attaches the VM
    disk as `/dev/vdb`. `is_in_rescue_mode` turns true after that boot. Rescue mode then blocks every
    change except a disk resize, a snapshot of the VM disk, a restart, power changes, and deletion.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApiErrorResponse | VirtualMachineResponse
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

) -> Response[ApiErrorResponse | VirtualMachineResponse]:
    """ Enable rescue mode

     `is_rescue_mode_enabled` turns true now. The next restart boots the rescue image and attaches the VM
    disk as `/dev/vdb`. `is_in_rescue_mode` turns true after that boot. Rescue mode then blocks every
    change except a disk resize, a snapshot of the VM disk, a restart, power changes, and deletion.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApiErrorResponse | VirtualMachineResponse]
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

) -> ApiErrorResponse | VirtualMachineResponse | None:
    """ Enable rescue mode

     `is_rescue_mode_enabled` turns true now. The next restart boots the rescue image and attaches the VM
    disk as `/dev/vdb`. `is_in_rescue_mode` turns true after that boot. Rescue mode then blocks every
    change except a disk resize, a snapshot of the VM disk, a restart, power changes, and deletion.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApiErrorResponse | VirtualMachineResponse
     """


    return (await asyncio_detailed(
        virtual_machine_id=virtual_machine_id,
client=client,
x_tenant_id=x_tenant_id,

    )).parsed
