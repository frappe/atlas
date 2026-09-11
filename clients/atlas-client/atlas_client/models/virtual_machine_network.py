from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="VirtualMachineNetwork")



@_attrs_define
class VirtualMachineNetwork:
    """ The addresses and network limits of one virtual machine.

        Attributes:
            egress (None | str):
            mac (None | str):
            mesh_ipv6 (None | str):
            private_network_throughput_mibps (int):
            public_ipv4 (None | str):
            public_network_throughput_mibps (int):
     """

    egress: None | str
    mac: None | str
    mesh_ipv6: None | str
    private_network_throughput_mibps: int
    public_ipv4: None | str
    public_network_throughput_mibps: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        egress: None | str
        egress = self.egress

        mac: None | str
        mac = self.mac

        mesh_ipv6: None | str
        mesh_ipv6 = self.mesh_ipv6

        private_network_throughput_mibps = self.private_network_throughput_mibps

        public_ipv4: None | str
        public_ipv4 = self.public_ipv4

        public_network_throughput_mibps = self.public_network_throughput_mibps


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "egress": egress,
            "mac": mac,
            "mesh_ipv6": mesh_ipv6,
            "private_network_throughput_mibps": private_network_throughput_mibps,
            "public_ipv4": public_ipv4,
            "public_network_throughput_mibps": public_network_throughput_mibps,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_egress(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        egress = _parse_egress(d.pop("egress"))


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


        public_network_throughput_mibps = d.pop("public_network_throughput_mibps")

        virtual_machine_network = cls(
            egress=egress,
            mac=mac,
            mesh_ipv6=mesh_ipv6,
            private_network_throughput_mibps=private_network_throughput_mibps,
            public_ipv4=public_ipv4,
            public_network_throughput_mibps=public_network_throughput_mibps,
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
