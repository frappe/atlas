from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.api_error_response import ApiErrorResponse
from ...models.page_host_response import PageHostResponse
from ...types import UNSET, Unset
from typing import cast



def _get_kwargs(
    *,
    offset: int | Unset = 0,
    limit: int | Unset = 20,
    tag: None | str | Unset = UNSET,
    x_tenant_id: int,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    headers["X-Tenant-ID"] = str(x_tenant_id)




    

    params: dict[str, Any] = {}

    params["offset"] = offset

    params["limit"] = limit

    json_tag: None | str | Unset
    if isinstance(tag, Unset):
        json_tag = UNSET
    else:
        json_tag = tag
    params["tag"] = json_tag


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/atlas/hosts",
        "params": params,
    }


    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> ApiErrorResponse | PageHostResponse | None:
    if response.status_code == 200:
        response_200 = PageHostResponse.from_dict(response.json())



        return response_200

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

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[ApiErrorResponse | PageHostResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    offset: int | Unset = 0,
    limit: int | Unset = 20,
    tag: None | str | Unset = UNSET,
    x_tenant_id: int,

) -> Response[ApiErrorResponse | PageHostResponse]:
    """ List hosts

     Returns the Metal hosts that are not deleted, with their tags, in title order. Pass `tag` to narrow
    them.

    Args:
        offset (int | Unset): Number of matching resources to skip. Default: 0.
        limit (int | Unset): Maximum number of resources to return. Default: 20.
        tag (None | str | Unset): Comma separated key:value tags. A resource must carry every
            pair, such as tag=os:Ubuntu,channel:lts.
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApiErrorResponse | PageHostResponse]
     """


    kwargs = _get_kwargs(
        offset=offset,
limit=limit,
tag=tag,
x_tenant_id=x_tenant_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient | Client,
    offset: int | Unset = 0,
    limit: int | Unset = 20,
    tag: None | str | Unset = UNSET,
    x_tenant_id: int,

) -> ApiErrorResponse | PageHostResponse | None:
    """ List hosts

     Returns the Metal hosts that are not deleted, with their tags, in title order. Pass `tag` to narrow
    them.

    Args:
        offset (int | Unset): Number of matching resources to skip. Default: 0.
        limit (int | Unset): Maximum number of resources to return. Default: 20.
        tag (None | str | Unset): Comma separated key:value tags. A resource must carry every
            pair, such as tag=os:Ubuntu,channel:lts.
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApiErrorResponse | PageHostResponse
     """


    return sync_detailed(
        client=client,
offset=offset,
limit=limit,
tag=tag,
x_tenant_id=x_tenant_id,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    offset: int | Unset = 0,
    limit: int | Unset = 20,
    tag: None | str | Unset = UNSET,
    x_tenant_id: int,

) -> Response[ApiErrorResponse | PageHostResponse]:
    """ List hosts

     Returns the Metal hosts that are not deleted, with their tags, in title order. Pass `tag` to narrow
    them.

    Args:
        offset (int | Unset): Number of matching resources to skip. Default: 0.
        limit (int | Unset): Maximum number of resources to return. Default: 20.
        tag (None | str | Unset): Comma separated key:value tags. A resource must carry every
            pair, such as tag=os:Ubuntu,channel:lts.
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApiErrorResponse | PageHostResponse]
     """


    kwargs = _get_kwargs(
        offset=offset,
limit=limit,
tag=tag,
x_tenant_id=x_tenant_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    offset: int | Unset = 0,
    limit: int | Unset = 20,
    tag: None | str | Unset = UNSET,
    x_tenant_id: int,

) -> ApiErrorResponse | PageHostResponse | None:
    """ List hosts

     Returns the Metal hosts that are not deleted, with their tags, in title order. Pass `tag` to narrow
    them.

    Args:
        offset (int | Unset): Number of matching resources to skip. Default: 0.
        limit (int | Unset): Maximum number of resources to return. Default: 20.
        tag (None | str | Unset): Comma separated key:value tags. A resource must carry every
            pair, such as tag=os:Ubuntu,channel:lts.
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApiErrorResponse | PageHostResponse
     """


    return (await asyncio_detailed(
        client=client,
offset=offset,
limit=limit,
tag=tag,
x_tenant_id=x_tenant_id,

    )).parsed
