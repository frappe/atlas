from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TypeVar

import httpx
from atlas_client.api.hosts import list_hosts
from atlas_client.api.vm_actions import (
    pause_virtual_machine,
    restart_virtual_machine,
    resume_virtual_machine,
    start_virtual_machine,
    stop_virtual_machine,
)
from atlas_client.api.vm_lifecycle import get_virtual_machine, list_virtual_machines
from atlas_client.client import AuthenticatedClient
from atlas_client.models import (
    ApiErrorResponse,
    HostResponse,
    PageHostResponse,
    PageVirtualMachineListResponse,
    VirtualMachineDetailResponse,
    VirtualMachineListResponse,
    VirtualMachineResponse,
)
from atlas_client.types import Response

Result = TypeVar("Result")


class CLIError(Exception):
    """An error that an operator can resolve."""


@dataclass(frozen=True)
class Configuration:
    """Connection settings for one command."""

    base_url: str
    token: str
    tenant_id: int

    @classmethod
    def from_arguments(cls, arguments: argparse.Namespace) -> Configuration:
        """Read settings from flags and environment variables."""
        base_url = arguments.base_url or os.environ.get("ATLAS_BASE_URL")
        if not base_url:
            raise CLIError("set --base-url or ATLAS_BASE_URL")

        token = os.environ.get("ATLAS_TOKEN", "")
        if not token or not token.isascii() or any(character.isspace() for character in token):
            raise CLIError("set ATLAS_TOKEN to a Bearer token")

        tenant = arguments.tenant_id
        if tenant is None:
            try:
                tenant = _tenant_id(os.environ.get("ATLAS_TENANT_ID", ""))
            except (argparse.ArgumentTypeError, ValueError) as error:
                raise CLIError("set ATLAS_TENANT_ID to an integer between 0 and 4294967295") from error

        return cls(_base_url(base_url), token, tenant)


def _base_url(value: str) -> str:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as error:
        raise CLIError("the Atlas URL is invalid") from error
    if url.scheme not in ("http", "https") or not url.host or url.userinfo or url.query or url.fragment:
        raise CLIError("use an HTTP or HTTPS Atlas URL without credentials, a query, or a fragment")
    return str(url).rstrip("/")


def _tenant_id(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 4_294_967_295:
        raise argparse.ArgumentTypeError("must be between 0 and 4294967295")
    return parsed


def _page_size(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("must be between 1 and 100")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atlasctl", description="Operate VMs through the Atlas API.")
    parser.add_argument("--base-url", help="Atlas URL, or use ATLAS_BASE_URL.")
    parser.add_argument("--tenant-id", type=_tenant_id, help="Tenant ID, or use ATLAS_TENANT_ID.")

    output = argparse.ArgumentParser(add_help=False)
    output.add_argument("--json", action="store_true", help="Print JSON.")
    listing = argparse.ArgumentParser(add_help=False, parents=[output])
    listing.add_argument("--limit", type=_page_size, default=100, help="Page size, from 1 to 100.")
    listing.add_argument("--tag", help="Filter by key:value tags.")

    resources = parser.add_subparsers(dest="resource", required=True)
    hosts = resources.add_parser("hosts", help="Inspect Metal hosts.")
    host_commands = hosts.add_subparsers(dest="command", required=True)
    hosts_list = host_commands.add_parser("list", parents=[listing], help="List Metal hosts.")
    hosts_list.set_defaults(handler=_list_hosts)

    virtual_machines = resources.add_parser("vms", help="Operate virtual machines.")
    commands = virtual_machines.add_subparsers(dest="command", required=True)
    vm_list = commands.add_parser("list", parents=[listing], help="List virtual machines.")
    vm_list.set_defaults(handler=_list_virtual_machines)
    vm_get = commands.add_parser("get", parents=[output], help="Show one virtual machine.")
    vm_get.add_argument("virtual_machine_id")
    vm_get.set_defaults(handler=_get_virtual_machine)

    for action, operation in (
        ("start", start_virtual_machine.sync_detailed),
        ("stop", stop_virtual_machine.sync_detailed),
        ("pause", pause_virtual_machine.sync_detailed),
        ("resume", resume_virtual_machine.sync_detailed),
        ("restart", restart_virtual_machine.sync_detailed),
    ):
        command = commands.add_parser(action, parents=[output], help=f"Request a VM {action}.")
        command.add_argument("virtual_machine_id")
        command.set_defaults(handler=_virtual_machine_action, operation=operation)

    return parser


def _request(operation: Callable[..., Response[Result]], **arguments: object) -> Response[Result]:
    try:
        return operation(**arguments)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise CLIError("Atlas API returned an invalid response") from error


def _expect(response: Response[object], expected_type: type[Result]) -> Result:
    if not 200 <= response.status_code < 300:
        message = f"Atlas API returned HTTP {response.status_code}"
        if isinstance(response.parsed, ApiErrorResponse):
            message += f": {response.parsed.error.message}"
        raise CLIError(message)

    if not isinstance(response.parsed, expected_type):
        raise CLIError("Atlas API returned an invalid response")
    return response.parsed


def _fetch_pages(
    arguments: argparse.Namespace,
    client: AuthenticatedClient,
    operation: Callable[..., Response[object]],
    page_type: type[PageHostResponse] | type[PageVirtualMachineListResponse],
) -> list[HostResponse] | list[VirtualMachineListResponse]:
    items = []
    offset = 0
    while True:
        response = _request(
            operation,
            client=client,
            offset=offset,
            limit=arguments.limit,
            tag=arguments.tag,
            x_tenant_id=arguments.tenant_id,
        )
        page = _expect(response, page_type)
        if (
            page.offset != offset
            or page.limit != arguments.limit
            or type(page.has_more) is not bool
            or (page.has_more and not page.items)
        ):
            raise CLIError("Atlas API returned invalid pagination")

        items.extend(page.items)
        if not page.has_more:
            return items
        offset += page.limit


def _list_hosts(arguments: argparse.Namespace, client: AuthenticatedClient) -> None:
    items = _fetch_pages(arguments, client, list_hosts.sync_detailed, PageHostResponse)
    if arguments.json:
        print(json.dumps({"items": [item.to_dict() for item in items]}, indent=2))
        return

    _print_table(
        ("ID", "TITLE", "STATUS", "TAGS"),
        (
            (
                item.id,
                item.title,
                item.status,
                ", ".join(f"{key}={value}" for key, value in item.tags.to_dict().items()),
            )
            for item in items
        ),
    )


def _list_virtual_machines(arguments: argparse.Namespace, client: AuthenticatedClient) -> None:
    items = _fetch_pages(arguments, client, list_virtual_machines.sync_detailed, PageVirtualMachineListResponse)
    if arguments.json:
        print(json.dumps({"items": [item.to_dict() for item in items]}, indent=2))
        return

    _print_table(
        ("ID", "STATE", "CPU", "MEMORY", "DISK", "PUBLIC IPV4"),
        (
            (
                item.id,
                item.last_known_state,
                f"{item.cpu_millicores}m",
                f"{item.memory_mib} MiB",
                f"{item.disk_mib} MiB",
                item.public_ipv4.prefix.partition("/")[0] if item.public_ipv4 else "-",
            )
            for item in items
        ),
    )


def _get_virtual_machine(arguments: argparse.Namespace, client: AuthenticatedClient) -> None:
    response = _request(
        get_virtual_machine.sync_detailed,
        virtual_machine_id=arguments.virtual_machine_id,
        client=client,
        x_tenant_id=arguments.tenant_id,
    )
    detail = _expect(response, VirtualMachineDetailResponse)
    if arguments.json:
        print(json.dumps(detail.to_dict(), indent=2))
        return

    print(f"id: {detail.id}")
    print(f"desired state: {detail.desired_state or 'unknown'}")
    print(f"current state: {detail.current_state}")
    print(f"image: {detail.image_id}")
    print(f"CPU: {detail.compute.cpu_millicores}m")
    print(f"memory: {detail.compute.memory_mib} MiB")
    print(f"disk: {detail.disk.size_mib} MiB")
    if detail.error:
        print(f"error: {detail.error}")


def _virtual_machine_action(arguments: argparse.Namespace, client: AuthenticatedClient) -> None:
    response = _request(
        arguments.operation,
        virtual_machine_id=arguments.virtual_machine_id,
        client=client,
        x_tenant_id=arguments.tenant_id,
    )
    virtual_machine = _expect(response, VirtualMachineResponse)
    if arguments.json:
        print(json.dumps(virtual_machine.to_dict(), indent=2))
    else:
        print(f"{arguments.command} requested for {virtual_machine.id}")


def _print_table(columns: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    materialized_rows = [tuple(str(value) for value in row) for row in rows]
    widths = [len(column) for column in columns]
    for row in materialized_rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    print("  ".join(column.ljust(widths[index]) for index, column in enumerate(columns)))
    for row in materialized_rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def main(arguments: Sequence[str] | None = None) -> int:
    """Run one operator command and return its exit status."""
    parsed = _build_parser().parse_args(arguments)
    try:
        configuration = Configuration.from_arguments(parsed)
        parsed.tenant_id = configuration.tenant_id
        with AuthenticatedClient(
            base_url=configuration.base_url,
            token=configuration.token,
            timeout=httpx.Timeout(30.0),
        ) as client:
            parsed.handler(parsed, client)
        return 0
    except CLIError as error:
        message = str(error)
    except httpx.TimeoutException:
        message = "request timed out; check the VM state before you repeat an action"
    except httpx.HTTPError:
        message = "could not complete the Atlas API request; check the connection and Atlas URL"

    token = os.environ.get("ATLAS_TOKEN")
    if token:
        message = message.replace(token, "[redacted]")
    print(f"atlasctl: {message}", file=sys.stderr)
    return 2
