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
            baseline_cpu_millicores (int | Unset): CPU a burstable VM can always use. Zero when not burstable. Default: 0.
            coma_after_idle_seconds (int | Unset): Idle time before disk offload. Zero disables it. Default: 0.
            deep_sleep_after_idle_seconds (int | Unset): Idle time before memory offload. Zero disables it. Default: 0.
            is_burstable (bool | Unset): Whether the VM can use CPU above its baseline. Default: False.
            sliding_window_minutes (int | Unset): How far back average CPU use is measured. Zero when not burstable.
                Default: 0.
     """

    cpu_millicores: int
    memory_mib: int
    sleep_after_idle_seconds: int
    baseline_cpu_millicores: int | Unset = 0
    coma_after_idle_seconds: int | Unset = 0
    deep_sleep_after_idle_seconds: int | Unset = 0
    is_burstable: bool | Unset = False
    sliding_window_minutes: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        cpu_millicores = self.cpu_millicores

        memory_mib = self.memory_mib

        sleep_after_idle_seconds = self.sleep_after_idle_seconds

        baseline_cpu_millicores = self.baseline_cpu_millicores

        coma_after_idle_seconds = self.coma_after_idle_seconds

        deep_sleep_after_idle_seconds = self.deep_sleep_after_idle_seconds

        is_burstable = self.is_burstable

        sliding_window_minutes = self.sliding_window_minutes


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "cpu_millicores": cpu_millicores,
            "memory_mib": memory_mib,
            "sleep_after_idle_seconds": sleep_after_idle_seconds,
        })
        if baseline_cpu_millicores is not UNSET:
            field_dict["baseline_cpu_millicores"] = baseline_cpu_millicores
        if coma_after_idle_seconds is not UNSET:
            field_dict["coma_after_idle_seconds"] = coma_after_idle_seconds
        if deep_sleep_after_idle_seconds is not UNSET:
            field_dict["deep_sleep_after_idle_seconds"] = deep_sleep_after_idle_seconds
        if is_burstable is not UNSET:
            field_dict["is_burstable"] = is_burstable
        if sliding_window_minutes is not UNSET:
            field_dict["sliding_window_minutes"] = sliding_window_minutes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        cpu_millicores = d.pop("cpu_millicores")

        memory_mib = d.pop("memory_mib")

        sleep_after_idle_seconds = d.pop("sleep_after_idle_seconds")

        baseline_cpu_millicores = d.pop("baseline_cpu_millicores", UNSET)

        coma_after_idle_seconds = d.pop("coma_after_idle_seconds", UNSET)

        deep_sleep_after_idle_seconds = d.pop("deep_sleep_after_idle_seconds", UNSET)

        is_burstable = d.pop("is_burstable", UNSET)

        sliding_window_minutes = d.pop("sliding_window_minutes", UNSET)

        virtual_machine_compute = cls(
            cpu_millicores=cpu_millicores,
            memory_mib=memory_mib,
            sleep_after_idle_seconds=sleep_after_idle_seconds,
            baseline_cpu_millicores=baseline_cpu_millicores,
            coma_after_idle_seconds=coma_after_idle_seconds,
            deep_sleep_after_idle_seconds=deep_sleep_after_idle_seconds,
            is_burstable=is_burstable,
            sliding_window_minutes=sliding_window_minutes,
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
