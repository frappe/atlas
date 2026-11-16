from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset






T = TypeVar("T", bound="VirtualMachineGuestFilesystem")



@_attrs_define
class VirtualMachineGuestFilesystem:
    """ Size and use of the guest root file system.

        Attributes:
            inodes_total (int | Unset):  Default: 0.
            inodes_used (int | Unset):  Default: 0.
            size_bytes (int | Unset):  Default: 0.
            used_bytes (int | Unset):  Default: 0.
     """

    inodes_total: int | Unset = 0
    inodes_used: int | Unset = 0
    size_bytes: int | Unset = 0
    used_bytes: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        inodes_total = self.inodes_total

        inodes_used = self.inodes_used

        size_bytes = self.size_bytes

        used_bytes = self.used_bytes


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
        })
        if inodes_total is not UNSET:
            field_dict["inodes_total"] = inodes_total
        if inodes_used is not UNSET:
            field_dict["inodes_used"] = inodes_used
        if size_bytes is not UNSET:
            field_dict["size_bytes"] = size_bytes
        if used_bytes is not UNSET:
            field_dict["used_bytes"] = used_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        inodes_total = d.pop("inodes_total", UNSET)

        inodes_used = d.pop("inodes_used", UNSET)

        size_bytes = d.pop("size_bytes", UNSET)

        used_bytes = d.pop("used_bytes", UNSET)

        virtual_machine_guest_filesystem = cls(
            inodes_total=inodes_total,
            inodes_used=inodes_used,
            size_bytes=size_bytes,
            used_bytes=used_bytes,
        )


        virtual_machine_guest_filesystem.additional_properties = d
        return virtual_machine_guest_filesystem

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
