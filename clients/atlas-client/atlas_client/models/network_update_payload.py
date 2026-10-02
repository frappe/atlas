from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.firewall_update_payload import FirewallUpdatePayload





T = TypeVar("T", bound="NetworkUpdatePayload")



@_attrs_define
class NetworkUpdatePayload:
    """ New IPv4 internet access, network rate limits, or firewall fields.

        Attributes:
            firewall (FirewallUpdatePayload | None | Unset): Firewall fields to replace.
            ipv4_internet_access (bool | Unset): Reach the IPv4 internet through host NAT. A public IPv4 address needs it.
            private_network_throughput_mibps (int | None | Unset): New private network throughput limit in MiB/s. Zero
                removes the limit.
            public_network_throughput_mibps (int | None | Unset): New public network throughput limit in MiB/s. Zero removes
                the limit.
            wireguard_gateway_access (bool | Unset): Let customer devices on the tenant's WireGuard gateways reach the VM. A
                network gateway cannot use it. Default: False.
     """

    firewall: FirewallUpdatePayload | None | Unset = UNSET
    ipv4_internet_access: bool | Unset = UNSET
    private_network_throughput_mibps: int | None | Unset = UNSET
    public_network_throughput_mibps: int | None | Unset = UNSET
    wireguard_gateway_access: bool | Unset = False





    def to_dict(self) -> dict[str, Any]:
        from ..models.firewall_update_payload import FirewallUpdatePayload # noqa: PLC0415
        firewall: dict[str, Any] | None | Unset
        if isinstance(self.firewall, Unset):
            firewall = UNSET
        elif isinstance(self.firewall, FirewallUpdatePayload):
            firewall = self.firewall.to_dict()
        else:
            firewall = self.firewall

        ipv4_internet_access = self.ipv4_internet_access

        private_network_throughput_mibps: int | None | Unset
        if isinstance(self.private_network_throughput_mibps, Unset):
            private_network_throughput_mibps = UNSET
        else:
            private_network_throughput_mibps = self.private_network_throughput_mibps

        public_network_throughput_mibps: int | None | Unset
        if isinstance(self.public_network_throughput_mibps, Unset):
            public_network_throughput_mibps = UNSET
        else:
            public_network_throughput_mibps = self.public_network_throughput_mibps

        wireguard_gateway_access = self.wireguard_gateway_access


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if firewall is not UNSET:
            field_dict["firewall"] = firewall
        if ipv4_internet_access is not UNSET:
            field_dict["ipv4_internet_access"] = ipv4_internet_access
        if private_network_throughput_mibps is not UNSET:
            field_dict["private_network_throughput_mibps"] = private_network_throughput_mibps
        if public_network_throughput_mibps is not UNSET:
            field_dict["public_network_throughput_mibps"] = public_network_throughput_mibps
        if wireguard_gateway_access is not UNSET:
            field_dict["wireguard_gateway_access"] = wireguard_gateway_access

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.firewall_update_payload import FirewallUpdatePayload # noqa: PLC0415
        d = dict(src_dict)
        def _parse_firewall(data: object) -> FirewallUpdatePayload | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                firewall_type_0 = FirewallUpdatePayload.from_dict(data)



                return firewall_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FirewallUpdatePayload | None | Unset, data)

        firewall = _parse_firewall(d.pop("firewall", UNSET))


        ipv4_internet_access = d.pop("ipv4_internet_access", UNSET)

        def _parse_private_network_throughput_mibps(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        private_network_throughput_mibps = _parse_private_network_throughput_mibps(d.pop("private_network_throughput_mibps", UNSET))


        def _parse_public_network_throughput_mibps(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        public_network_throughput_mibps = _parse_public_network_throughput_mibps(d.pop("public_network_throughput_mibps", UNSET))


        wireguard_gateway_access = d.pop("wireguard_gateway_access", UNSET)

        network_update_payload = cls(
            firewall=firewall,
            ipv4_internet_access=ipv4_internet_access,
            private_network_throughput_mibps=private_network_throughput_mibps,
            public_network_throughput_mibps=public_network_throughput_mibps,
            wireguard_gateway_access=wireguard_gateway_access,
        )

        return network_update_payload

