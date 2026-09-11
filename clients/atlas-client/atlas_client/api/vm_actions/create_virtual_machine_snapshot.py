from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.image_response import ImageResponse
from ...models.snapshot_payload import SnapshotPayload
from typing import cast



def _get_kwargs(
    virtual_machine_id: str,
    *,
    body: SnapshotPayload,
    x_tenant_id: int,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    headers["X-Tenant-ID"] = str(x_tenant_id)




    

    

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/atlas/virtual-machines/{virtual_machine_id}/actions/snapshot".format(virtual_machine_id=quote(str(virtual_machine_id), safe=""),),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> ImageResponse | None:
    if response.status_code == 201:
        response_201 = ImageResponse.from_dict(response.json())



        return response_201

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[ImageResponse]:
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
    body: SnapshotPayload,
    x_tenant_id: int,

) -> Response[ImageResponse]:
    """ Create snapshot

     Creates a reusable Machine image from the current VM disk. The new image belongs to the same tenant.

    If `memory_snapshot` is true, Atlas also records the VM shape for compatible warm starts. Only
    tenant 0 can set `image_type` to `system`, which shares the image with every tenant, and only tenant
    0 can set `cache_image` and `memory_snapshot`. These values cannot change after creation.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (SnapshotPayload): Values that create one image from a virtual machine.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ImageResponse]
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
    body: SnapshotPayload,
    x_tenant_id: int,

) -> ImageResponse | None:
    """ Create snapshot

     Creates a reusable Machine image from the current VM disk. The new image belongs to the same tenant.

    If `memory_snapshot` is true, Atlas also records the VM shape for compatible warm starts. Only
    tenant 0 can set `image_type` to `system`, which shares the image with every tenant, and only tenant
    0 can set `cache_image` and `memory_snapshot`. These values cannot change after creation.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (SnapshotPayload): Values that create one image from a virtual machine.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ImageResponse
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
    body: SnapshotPayload,
    x_tenant_id: int,

) -> Response[ImageResponse]:
    """ Create snapshot

     Creates a reusable Machine image from the current VM disk. The new image belongs to the same tenant.

    If `memory_snapshot` is true, Atlas also records the VM shape for compatible warm starts. Only
    tenant 0 can set `image_type` to `system`, which shares the image with every tenant, and only tenant
    0 can set `cache_image` and `memory_snapshot`. These values cannot change after creation.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (SnapshotPayload): Values that create one image from a virtual machine.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ImageResponse]
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
    body: SnapshotPayload,
    x_tenant_id: int,

) -> ImageResponse | None:
    """ Create snapshot

     Creates a reusable Machine image from the current VM disk. The new image belongs to the same tenant.

    If `memory_snapshot` is true, Atlas also records the VM shape for compatible warm starts. Only
    tenant 0 can set `image_type` to `system`, which shares the image with every tenant, and only tenant
    0 can set `cache_image` and `memory_snapshot`. These values cannot change after creation.

    Args:
        virtual_machine_id (str):
        x_tenant_id (int):
        body (SnapshotPayload): Values that create one image from a virtual machine.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ImageResponse
     """


    return (await asyncio_detailed(
        virtual_machine_id=virtual_machine_id,
client=client,
body=body,
x_tenant_id=x_tenant_id,

    )).parsed
