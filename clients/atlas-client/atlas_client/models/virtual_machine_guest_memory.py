from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset






T = TypeVar("T", bound="VirtualMachineGuestMemory")



@_attrs_define
class VirtualMachineGuestMemory:
    """ Memory use that the guest kernel reports in `/proc/meminfo`.

        Attributes:
            available_bytes (int | Unset): Memory that new work can use without swap, including reclaimable cache. Default:
                0.
            cached_bytes (int | Unset): Memory that the page cache uses. Default: 0.
            free_bytes (int | Unset): Memory that nothing uses, not even the page cache. Default: 0.
            swap_free_bytes (int | Unset):  Default: 0.
            swap_total_bytes (int | Unset):  Default: 0.
            total_bytes (int | Unset):  Default: 0.
     """

    available_bytes: int | Unset = 0
    cached_bytes: int | Unset = 0
    free_bytes: int | Unset = 0
    swap_free_bytes: int | Unset = 0
    swap_total_bytes: int | Unset = 0
    total_bytes: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        available_bytes = self.available_bytes

        cached_bytes = self.cached_bytes

        free_bytes = self.free_bytes

        swap_free_bytes = self.swap_free_bytes

        swap_total_bytes = self.swap_total_bytes

        total_bytes = self.total_bytes


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
        })
        if available_bytes is not UNSET:
            field_dict["available_bytes"] = available_bytes
        if cached_bytes is not UNSET:
            field_dict["cached_bytes"] = cached_bytes
        if free_bytes is not UNSET:
            field_dict["free_bytes"] = free_bytes
        if swap_free_bytes is not UNSET:
            field_dict["swap_free_bytes"] = swap_free_bytes
        if swap_total_bytes is not UNSET:
            field_dict["swap_total_bytes"] = swap_total_bytes
        if total_bytes is not UNSET:
            field_dict["total_bytes"] = total_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        available_bytes = d.pop("available_bytes", UNSET)

        cached_bytes = d.pop("cached_bytes", UNSET)

        free_bytes = d.pop("free_bytes", UNSET)

        swap_free_bytes = d.pop("swap_free_bytes", UNSET)

        swap_total_bytes = d.pop("swap_total_bytes", UNSET)

        total_bytes = d.pop("total_bytes", UNSET)

        virtual_machine_guest_memory = cls(
            available_bytes=available_bytes,
            cached_bytes=cached_bytes,
            free_bytes=free_bytes,
            swap_free_bytes=swap_free_bytes,
            swap_total_bytes=swap_total_bytes,
            total_bytes=total_bytes,
        )


        virtual_machine_guest_memory.additional_properties = d
        return virtual_machine_guest_memory

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
