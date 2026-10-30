from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset






T = TypeVar("T", bound="VirtualMachineCompute")



@_attrs_define
class VirtualMachineCompute:
    """ The compute shape of one virtual machine.

        Attributes:
            cpu_millicores (int): CPU capacity in millicores.
            memory_mib (int): Memory capacity in MiB.
            sleep_after_idle_seconds (int): Idle time before automatic stop. Zero disables it.
            coma_after_idle_seconds (int | Unset): Idle time before disk offload. Zero disables it. Default: 0.
            deep_sleep_after_idle_seconds (int | Unset): Idle time before memory offload. Zero disables it. Default: 0.
     """

    cpu_millicores: int
    memory_mib: int
    sleep_after_idle_seconds: int
    coma_after_idle_seconds: int | Unset = 0
    deep_sleep_after_idle_seconds: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        cpu_millicores = self.cpu_millicores

        memory_mib = self.memory_mib

        sleep_after_idle_seconds = self.sleep_after_idle_seconds

        coma_after_idle_seconds = self.coma_after_idle_seconds

        deep_sleep_after_idle_seconds = self.deep_sleep_after_idle_seconds


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "cpu_millicores": cpu_millicores,
            "memory_mib": memory_mib,
            "sleep_after_idle_seconds": sleep_after_idle_seconds,
        })
        if coma_after_idle_seconds is not UNSET:
            field_dict["coma_after_idle_seconds"] = coma_after_idle_seconds
        if deep_sleep_after_idle_seconds is not UNSET:
            field_dict["deep_sleep_after_idle_seconds"] = deep_sleep_after_idle_seconds

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        cpu_millicores = d.pop("cpu_millicores")

        memory_mib = d.pop("memory_mib")

        sleep_after_idle_seconds = d.pop("sleep_after_idle_seconds")

        coma_after_idle_seconds = d.pop("coma_after_idle_seconds", UNSET)

        deep_sleep_after_idle_seconds = d.pop("deep_sleep_after_idle_seconds", UNSET)

        virtual_machine_compute = cls(
            cpu_millicores=cpu_millicores,
            memory_mib=memory_mib,
            sleep_after_idle_seconds=sleep_after_idle_seconds,
            coma_after_idle_seconds=coma_after_idle_seconds,
            deep_sleep_after_idle_seconds=deep_sleep_after_idle_seconds,
        )


        virtual_machine_compute.additional_properties = d
        return virtual_machine_compute

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
