from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="ResizePayload")



@_attrs_define
class ResizePayload:
    """ VM resource, idle shutdown, and CPU burst changes.

        Attributes:
            baseline_cpu_millicores (int | None | Unset): New CPU a burstable VM can always use, at most its CPU. Zero uses
                20% of CPU.
            coma_after_idle_seconds (int | None | Unset): New idle time before Atlas offloads the disk. Zero disables it.
            cpu_millicores (int | None | Unset): New CPU capacity in millicores.
            deep_sleep_after_idle_seconds (int | None | Unset): New idle time before Atlas offloads saved memory. Zero
                disables it.
            disk_mib (int | None | Unset): New root disk capacity in MiB.
            is_burstable (bool | None | Unset): Whether the VM can use CPU above its baseline. False clears the baseline and
                window.
            memory_mib (int | None | Unset): New memory capacity in MiB.
            sleep_after_idle_seconds (int | None | Unset): New idle time before automatic stop. Zero disables it.
            sliding_window_minutes (int | None | Unset): New sliding window of a burstable VM in minutes. Zero uses 60.
     """

    baseline_cpu_millicores: int | None | Unset = UNSET
    coma_after_idle_seconds: int | None | Unset = UNSET
    cpu_millicores: int | None | Unset = UNSET
    deep_sleep_after_idle_seconds: int | None | Unset = UNSET
    disk_mib: int | None | Unset = UNSET
    is_burstable: bool | None | Unset = UNSET
    memory_mib: int | None | Unset = UNSET
    sleep_after_idle_seconds: int | None | Unset = UNSET
    sliding_window_minutes: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        baseline_cpu_millicores: int | None | Unset
        if isinstance(self.baseline_cpu_millicores, Unset):
            baseline_cpu_millicores = UNSET
        else:
            baseline_cpu_millicores = self.baseline_cpu_millicores

        coma_after_idle_seconds: int | None | Unset
        if isinstance(self.coma_after_idle_seconds, Unset):
            coma_after_idle_seconds = UNSET
        else:
            coma_after_idle_seconds = self.coma_after_idle_seconds

        cpu_millicores: int | None | Unset
        if isinstance(self.cpu_millicores, Unset):
            cpu_millicores = UNSET
        else:
            cpu_millicores = self.cpu_millicores

        deep_sleep_after_idle_seconds: int | None | Unset
        if isinstance(self.deep_sleep_after_idle_seconds, Unset):
            deep_sleep_after_idle_seconds = UNSET
        else:
            deep_sleep_after_idle_seconds = self.deep_sleep_after_idle_seconds

        disk_mib: int | None | Unset
        if isinstance(self.disk_mib, Unset):
            disk_mib = UNSET
        else:
            disk_mib = self.disk_mib

        is_burstable: bool | None | Unset
        if isinstance(self.is_burstable, Unset):
            is_burstable = UNSET
        else:
            is_burstable = self.is_burstable

        memory_mib: int | None | Unset
        if isinstance(self.memory_mib, Unset):
            memory_mib = UNSET
        else:
            memory_mib = self.memory_mib

        sleep_after_idle_seconds: int | None | Unset
        if isinstance(self.sleep_after_idle_seconds, Unset):
            sleep_after_idle_seconds = UNSET
        else:
            sleep_after_idle_seconds = self.sleep_after_idle_seconds

        sliding_window_minutes: int | None | Unset
        if isinstance(self.sliding_window_minutes, Unset):
            sliding_window_minutes = UNSET
        else:
            sliding_window_minutes = self.sliding_window_minutes


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if baseline_cpu_millicores is not UNSET:
            field_dict["baseline_cpu_millicores"] = baseline_cpu_millicores
        if coma_after_idle_seconds is not UNSET:
            field_dict["coma_after_idle_seconds"] = coma_after_idle_seconds
        if cpu_millicores is not UNSET:
            field_dict["cpu_millicores"] = cpu_millicores
        if deep_sleep_after_idle_seconds is not UNSET:
            field_dict["deep_sleep_after_idle_seconds"] = deep_sleep_after_idle_seconds
        if disk_mib is not UNSET:
            field_dict["disk_mib"] = disk_mib
        if is_burstable is not UNSET:
            field_dict["is_burstable"] = is_burstable
        if memory_mib is not UNSET:
            field_dict["memory_mib"] = memory_mib
        if sleep_after_idle_seconds is not UNSET:
            field_dict["sleep_after_idle_seconds"] = sleep_after_idle_seconds
        if sliding_window_minutes is not UNSET:
            field_dict["sliding_window_minutes"] = sliding_window_minutes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_baseline_cpu_millicores(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        baseline_cpu_millicores = _parse_baseline_cpu_millicores(d.pop("baseline_cpu_millicores", UNSET))


        def _parse_coma_after_idle_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        coma_after_idle_seconds = _parse_coma_after_idle_seconds(d.pop("coma_after_idle_seconds", UNSET))


        def _parse_cpu_millicores(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        cpu_millicores = _parse_cpu_millicores(d.pop("cpu_millicores", UNSET))


        def _parse_deep_sleep_after_idle_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        deep_sleep_after_idle_seconds = _parse_deep_sleep_after_idle_seconds(d.pop("deep_sleep_after_idle_seconds", UNSET))


        def _parse_disk_mib(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        disk_mib = _parse_disk_mib(d.pop("disk_mib", UNSET))


        def _parse_is_burstable(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        is_burstable = _parse_is_burstable(d.pop("is_burstable", UNSET))


        def _parse_memory_mib(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        memory_mib = _parse_memory_mib(d.pop("memory_mib", UNSET))


        def _parse_sleep_after_idle_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        sleep_after_idle_seconds = _parse_sleep_after_idle_seconds(d.pop("sleep_after_idle_seconds", UNSET))


        def _parse_sliding_window_minutes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        sliding_window_minutes = _parse_sliding_window_minutes(d.pop("sliding_window_minutes", UNSET))


        resize_payload = cls(
            baseline_cpu_millicores=baseline_cpu_millicores,
            coma_after_idle_seconds=coma_after_idle_seconds,
            cpu_millicores=cpu_millicores,
            deep_sleep_after_idle_seconds=deep_sleep_after_idle_seconds,
            disk_mib=disk_mib,
            is_burstable=is_burstable,
            memory_mib=memory_mib,
            sleep_after_idle_seconds=sleep_after_idle_seconds,
            sliding_window_minutes=sliding_window_minutes,
        )

        return resize_payload

