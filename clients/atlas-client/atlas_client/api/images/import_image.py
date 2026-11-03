from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.api_error_response import ApiErrorResponse
from ...models.image_import_payload import ImageImportPayload
from ...models.image_response import ImageResponse
from typing import cast



def _get_kwargs(
    *,
    body: ImageImportPayload,
    x_tenant_id: int,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    headers["X-Tenant-ID"] = str(x_tenant_id)




    

    

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/atlas/images/import",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> ApiErrorResponse | ImageResponse | None:
    if response.status_code == 202:
        response_202 = ImageResponse.from_dict(response.json())



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


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[ApiErrorResponse | ImageResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ImageImportPayload,
    x_tenant_id: int,

) -> Response[ApiErrorResponse | ImageResponse]:
    """ Import OCI image

     Builds an image of the calling tenant from a public OCI image in an allowed registry. Only tenant 0
    can set `image_type` to `system`, which shares the image with every tenant. A second request for the
    same image digest returns the image that already has it. The image boots with its own entrypoint,
    and metadata keys named `env.<NAME>` set its environment.

    Args:
        x_tenant_id (int):
        body (ImageImportPayload): The OCI image to import.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApiErrorResponse | ImageResponse]
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
    body: ImageImportPayload,
    x_tenant_id: int,

) -> ApiErrorResponse | ImageResponse | None:
    """ Import OCI image

     Builds an image of the calling tenant from a public OCI image in an allowed registry. Only tenant 0
    can set `image_type` to `system`, which shares the image with every tenant. A second request for the
    same image digest returns the image that already has it. The image boots with its own entrypoint,
    and metadata keys named `env.<NAME>` set its environment.

    Args:
        x_tenant_id (int):
        body (ImageImportPayload): The OCI image to import.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApiErrorResponse | ImageResponse
     """


    return sync_detailed(
        client=client,
body=body,
x_tenant_id=x_tenant_id,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ImageImportPayload,
    x_tenant_id: int,

) -> Response[ApiErrorResponse | ImageResponse]:
    """ Import OCI image

     Builds an image of the calling tenant from a public OCI image in an allowed registry. Only tenant 0
    can set `image_type` to `system`, which shares the image with every tenant. A second request for the
    same image digest returns the image that already has it. The image boots with its own entrypoint,
    and metadata keys named `env.<NAME>` set its environment.

    Args:
        x_tenant_id (int):
        body (ImageImportPayload): The OCI image to import.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApiErrorResponse | ImageResponse]
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
    body: ImageImportPayload,
    x_tenant_id: int,

) -> ApiErrorResponse | ImageResponse | None:
    """ Import OCI image

     Builds an image of the calling tenant from a public OCI image in an allowed registry. Only tenant 0
    can set `image_type` to `system`, which shares the image with every tenant. A second request for the
    same image digest returns the image that already has it. The image boots with its own entrypoint,
    and metadata keys named `env.<NAME>` set its environment.

    Args:
        x_tenant_id (int):
        body (ImageImportPayload): The OCI image to import.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApiErrorResponse | ImageResponse
     """


    return (await asyncio_detailed(
        client=client,
body=body,
x_tenant_id=x_tenant_id,

    )).parsed
