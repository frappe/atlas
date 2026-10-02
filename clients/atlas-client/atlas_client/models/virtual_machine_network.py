from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.firewall_response import FirewallResponse





T = TypeVar("T", bound="VirtualMachineNetwork")



@_attrs_define
class VirtualMachineNetwork:
    """ The addresses, internet access, and network limits of one virtual machine.

        Attributes:
            firewall (FirewallResponse): The complete desired firewall configuration.
            ipv4_internet_access (bool): Whether the guest reaches the IPv4 internet through host NAT.
            mac (None | str): Observed network interface MAC address, or null.
            mesh_ipv6 (None | str): Private WireGuard mesh IPv6 address, or null.
            private_network_throughput_mibps (int): Private network throughput limit in MiB/s.
            public_ipv4 (None | str): IPv4 address configured on the guest, or null.
            public_ipv6 (None | str): IPv6 prefix assigned to the guest, or null.
            public_network_throughput_mibps (int): Public network throughput limit in MiB/s.
            wireguard_gateway_access (bool): Whether customer devices on the tenant's WireGuard gateways reach the VM.
     """

    firewall: FirewallResponse
    ipv4_internet_access: bool
    mac: None | str
    mesh_ipv6: None | str
    private_network_throughput_mibps: int
    public_ipv4: None | str
    public_ipv6: None | str
    public_network_throughput_mibps: int
    wireguard_gateway_access: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.firewall_response import FirewallResponse # noqa: PLC0415
        firewall = self.firewall.to_dict()

        ipv4_internet_access = self.ipv4_internet_access

        mac: None | str
        mac = self.mac

        mesh_ipv6: None | str
        mesh_ipv6 = self.mesh_ipv6

        private_network_throughput_mibps = self.private_network_throughput_mibps

        public_ipv4: None | str
        public_ipv4 = self.public_ipv4

        public_ipv6: None | str
        public_ipv6 = self.public_ipv6

        public_network_throughput_mibps = self.public_network_throughput_mibps

        wireguard_gateway_access = self.wireguard_gateway_access


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "firewall": firewall,
            "ipv4_internet_access": ipv4_internet_access,
            "mac": mac,
            "mesh_ipv6": mesh_ipv6,
            "private_network_throughput_mibps": private_network_throughput_mibps,
            "public_ipv4": public_ipv4,
            "public_ipv6": public_ipv6,
            "public_network_throughput_mibps": public_network_throughput_mibps,
            "wireguard_gateway_access": wireguard_gateway_access,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.firewall_response import FirewallResponse # noqa: PLC0415
        d = dict(src_dict)
        firewall = FirewallResponse.from_dict(d.pop("firewall"))




        ipv4_internet_access = d.pop("ipv4_internet_access")

        def _parse_mac(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        mac = _parse_mac(d.pop("mac"))


        def _parse_mesh_ipv6(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        mesh_ipv6 = _parse_mesh_ipv6(d.pop("mesh_ipv6"))


        private_network_throughput_mibps = d.pop("private_network_throughput_mibps")

        def _parse_public_ipv4(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        public_ipv4 = _parse_public_ipv4(d.pop("public_ipv4"))


        def _parse_public_ipv6(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        public_ipv6 = _parse_public_ipv6(d.pop("public_ipv6"))


        public_network_throughput_mibps = d.pop("public_network_throughput_mibps")

        wireguard_gateway_access = d.pop("wireguard_gateway_access")

        virtual_machine_network = cls(
            firewall=firewall,
            ipv4_internet_access=ipv4_internet_access,
            mac=mac,
            mesh_ipv6=mesh_ipv6,
            private_network_throughput_mibps=private_network_throughput_mibps,
            public_ipv4=public_ipv4,
            public_ipv6=public_ipv6,
            public_network_throughput_mibps=public_network_throughput_mibps,
            wireguard_gateway_access=wireguard_gateway_access,
        )


        virtual_machine_network.additional_properties = d
        return virtual_machine_network

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
