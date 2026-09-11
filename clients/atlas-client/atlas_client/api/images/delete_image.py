from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.image_response import ImageResponse
from typing import cast



def _get_kwargs(
    image_id: str,
    *,
    x_tenant_id: int,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    headers["X-Tenant-ID"] = str(x_tenant_id)




    

    

    _kwargs: dict[str, Any] = {
        "method": "delete",
        "url": "/api/atlas/images/{image_id}".format(image_id=quote(str(image_id), safe=""),),
    }


    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Any | ImageResponse | None:
    if response.status_code == 202:
        response_202 = ImageResponse.from_dict(response.json())



        return response_202

    if response.status_code == 409:
        response_409 = cast(Any, None)
        return response_409

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[Any | ImageResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    image_id: str,
    *,
    client: AuthenticatedClient | Client,
    x_tenant_id: int,

) -> Response[Any | ImageResponse]:
    """ Delete image

     Starts deletion of an unused Available image that the tenant owns. A cleanup job removes its stored
    artifacts and remaining host snapshot data.

    Args:
        image_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ImageResponse]
     """


    kwargs = _get_kwargs(
        image_id=image_id,
x_tenant_id=x_tenant_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    image_id: str,
    *,
    client: AuthenticatedClient | Client,
    x_tenant_id: int,

) -> Any | ImageResponse | None:
    """ Delete image

     Starts deletion of an unused Available image that the tenant owns. A cleanup job removes its stored
    artifacts and remaining host snapshot data.

    Args:
        image_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ImageResponse
     """


    return sync_detailed(
        image_id=image_id,
client=client,
x_tenant_id=x_tenant_id,

    ).parsed

async def asyncio_detailed(
    image_id: str,
    *,
    client: AuthenticatedClient | Client,
    x_tenant_id: int,

) -> Response[Any | ImageResponse]:
    """ Delete image

     Starts deletion of an unused Available image that the tenant owns. A cleanup job removes its stored
    artifacts and remaining host snapshot data.

    Args:
        image_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ImageResponse]
     """


    kwargs = _get_kwargs(
        image_id=image_id,
x_tenant_id=x_tenant_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    image_id: str,
    *,
    client: AuthenticatedClient | Client,
    x_tenant_id: int,

) -> Any | ImageResponse | None:
    """ Delete image

     Starts deletion of an unused Available image that the tenant owns. A cleanup job removes its stored
    artifacts and remaining host snapshot data.

    Args:
        image_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ImageResponse
     """


    return (await asyncio_detailed(
        image_id=image_id,
client=client,
x_tenant_id=x_tenant_id,

    )).parsed
