from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="VirtualMachineDisk")



@_attrs_define
class VirtualMachineDisk:
    """ The disk size and its rate limits.

        Attributes:
            iops (int):
            size_mib (int):
            throughput_mibps (int):
            used_mib (int | None):
     """

    iops: int
    size_mib: int
    throughput_mibps: int
    used_mib: int | None
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        iops = self.iops

        size_mib = self.size_mib

        throughput_mibps = self.throughput_mibps

        used_mib: int | None
        used_mib = self.used_mib


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "iops": iops,
            "size_mib": size_mib,
            "throughput_mibps": throughput_mibps,
            "used_mib": used_mib,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        iops = d.pop("iops")

        size_mib = d.pop("size_mib")

        throughput_mibps = d.pop("throughput_mibps")

        def _parse_used_mib(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        used_mib = _parse_used_mib(d.pop("used_mib"))


        virtual_machine_disk = cls(
            iops=iops,
            size_mib=size_mib,
            throughput_mibps=throughput_mibps,
            used_mib=used_mib,
        )


        virtual_machine_disk.additional_properties = d
        return virtual_machine_disk

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
