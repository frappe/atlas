from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="VirtualMachineComputeUsage")



@_attrs_define
class VirtualMachineComputeUsage:
    """ Cumulative CPU time and current memory use.

        Attributes:
            cpu_microseconds (int): Cumulative CPU time since the guest's process started.
            memory_bytes (int): Memory in use inside the guest, in bytes. Without a guest report, the memory charged to the Firecracker cgroup.
     """

    cpu_microseconds: int
    memory_bytes: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        cpu_microseconds = self.cpu_microseconds

        memory_bytes = self.memory_bytes


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "cpu_microseconds": cpu_microseconds,
            "memory_bytes": memory_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        cpu_microseconds = d.pop("cpu_microseconds")

        memory_bytes = d.pop("memory_bytes")

        virtual_machine_compute_usage = cls(
            cpu_microseconds=cpu_microseconds,
            memory_bytes=memory_bytes,
        )


        virtual_machine_compute_usage.additional_properties = d
        return virtual_machine_compute_usage

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
