from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.api_error_response import ApiErrorResponse
from ...models.host_access_revoke_payload import HostAccessRevokePayload
from typing import cast



def _get_kwargs(
    host_id: str,
    *,
    body: HostAccessRevokePayload,
    x_tenant_id: int,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    headers["X-Tenant-ID"] = str(x_tenant_id)




    

    

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/atlas/hosts/{host_id}/access/revoke".format(host_id=quote(str(host_id), safe=""),),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Any | ApiErrorResponse | None:
    if response.status_code == 204:
        response_204 = cast(Any, None)
        return response_204

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

    if response.status_code == 503:
        response_503 = ApiErrorResponse.from_dict(response.json())



        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[Any | ApiErrorResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    host_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: HostAccessRevokePayload,
    x_tenant_id: int,

) -> Response[Any | ApiErrorResponse]:
    """ Revoke host access

     Closes one host, or every host when the ID is `all`, to one person now.

    Args:
        host_id (str):
        x_tenant_id (int):
        body (HostAccessRevokePayload): Close one host, or every host, to one person now.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ApiErrorResponse]
     """


    kwargs = _get_kwargs(
        host_id=host_id,
body=body,
x_tenant_id=x_tenant_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    host_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: HostAccessRevokePayload,
    x_tenant_id: int,

) -> Any | ApiErrorResponse | None:
    """ Revoke host access

     Closes one host, or every host when the ID is `all`, to one person now.

    Args:
        host_id (str):
        x_tenant_id (int):
        body (HostAccessRevokePayload): Close one host, or every host, to one person now.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ApiErrorResponse
     """


    return sync_detailed(
        host_id=host_id,
client=client,
body=body,
x_tenant_id=x_tenant_id,

    ).parsed

async def asyncio_detailed(
    host_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: HostAccessRevokePayload,
    x_tenant_id: int,

) -> Response[Any | ApiErrorResponse]:
    """ Revoke host access

     Closes one host, or every host when the ID is `all`, to one person now.

    Args:
        host_id (str):
        x_tenant_id (int):
        body (HostAccessRevokePayload): Close one host, or every host, to one person now.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ApiErrorResponse]
     """


    kwargs = _get_kwargs(
        host_id=host_id,
body=body,
x_tenant_id=x_tenant_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    host_id: str,
    *,
    client: AuthenticatedClient | Client,
    body: HostAccessRevokePayload,
    x_tenant_id: int,

) -> Any | ApiErrorResponse | None:
    """ Revoke host access

     Closes one host, or every host when the ID is `all`, to one person now.

    Args:
        host_id (str):
        x_tenant_id (int):
        body (HostAccessRevokePayload): Close one host, or every host, to one person now.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ApiErrorResponse
     """


    return (await asyncio_detailed(
        host_id=host_id,
client=client,
body=body,
x_tenant_id=x_tenant_id,

    )).parsed
