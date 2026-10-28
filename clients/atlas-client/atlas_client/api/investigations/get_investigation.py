from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.api_error_response import ApiErrorResponse
from ...models.investigation_response import InvestigationResponse
from typing import cast



def _get_kwargs(
    investigation_id: str,
    *,
    x_tenant_id: int,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    headers["X-Tenant-ID"] = str(x_tenant_id)




    

    

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/atlas/investigations/{investigation_id}".format(investigation_id=quote(str(investigation_id), safe=""),),
    }


    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> ApiErrorResponse | InvestigationResponse | None:
    if response.status_code == 200:
        response_200 = InvestigationResponse.from_dict(response.json())



        return response_200

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

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: AuthenticatedClient | Client, response: httpx.Response) -> Response[ApiErrorResponse | InvestigationResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    investigation_id: str,
    *,
    client: AuthenticatedClient | Client,
    x_tenant_id: int,

) -> Response[ApiErrorResponse | InvestigationResponse]:
    """ Get investigation

     Returns the status and the report. Atlas masks credentials. The audit log of tool calls stays in the
    Atlas Desk.

    Args:
        investigation_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApiErrorResponse | InvestigationResponse]
     """


    kwargs = _get_kwargs(
        investigation_id=investigation_id,
x_tenant_id=x_tenant_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    investigation_id: str,
    *,
    client: AuthenticatedClient | Client,
    x_tenant_id: int,

) -> ApiErrorResponse | InvestigationResponse | None:
    """ Get investigation

     Returns the status and the report. Atlas masks credentials. The audit log of tool calls stays in the
    Atlas Desk.

    Args:
        investigation_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApiErrorResponse | InvestigationResponse
     """


    return sync_detailed(
        investigation_id=investigation_id,
client=client,
x_tenant_id=x_tenant_id,

    ).parsed

async def asyncio_detailed(
    investigation_id: str,
    *,
    client: AuthenticatedClient | Client,
    x_tenant_id: int,

) -> Response[ApiErrorResponse | InvestigationResponse]:
    """ Get investigation

     Returns the status and the report. Atlas masks credentials. The audit log of tool calls stays in the
    Atlas Desk.

    Args:
        investigation_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApiErrorResponse | InvestigationResponse]
     """


    kwargs = _get_kwargs(
        investigation_id=investigation_id,
x_tenant_id=x_tenant_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    investigation_id: str,
    *,
    client: AuthenticatedClient | Client,
    x_tenant_id: int,

) -> ApiErrorResponse | InvestigationResponse | None:
    """ Get investigation

     Returns the status and the report. Atlas masks credentials. The audit log of tool calls stays in the
    Atlas Desk.

    Args:
        investigation_id (str):
        x_tenant_id (int):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApiErrorResponse | InvestigationResponse
     """


    return (await asyncio_detailed(
        investigation_id=investigation_id,
client=client,
x_tenant_id=x_tenant_id,

    )).parsed
