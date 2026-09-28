from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="VirtualMachineDiskUsage")



@_attrs_define
class VirtualMachineDiskUsage:
    """ The disk's requested size and its use as of the last reconcile pass.

        Attributes:
            size_mib (int): Requested disk size.
            used_mib (int): Disk use as of the last reconcile pass.
     """

    size_mib: int
    used_mib: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        size_mib = self.size_mib

        used_mib = self.used_mib


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "size_mib": size_mib,
            "used_mib": used_mib,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        size_mib = d.pop("size_mib")

        used_mib = d.pop("used_mib")

        virtual_machine_disk_usage = cls(
            size_mib=size_mib,
            used_mib=used_mib,
        )


        virtual_machine_disk_usage.additional_properties = d
        return virtual_machine_disk_usage

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
