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
    """ VM resource and idle shutdown changes.

        Attributes:
            cpu_millicores (int | None | Unset):
            disk_mib (int | None | Unset):
            memory_mib (int | None | Unset):
            sleep_after_idle_seconds (int | None | Unset):
     """

    cpu_millicores: int | None | Unset = UNSET
    disk_mib: int | None | Unset = UNSET
    memory_mib: int | None | Unset = UNSET
    sleep_after_idle_seconds: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        cpu_millicores: int | None | Unset
        if isinstance(self.cpu_millicores, Unset):
            cpu_millicores = UNSET
        else:
            cpu_millicores = self.cpu_millicores

        disk_mib: int | None | Unset
        if isinstance(self.disk_mib, Unset):
            disk_mib = UNSET
        else:
            disk_mib = self.disk_mib

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


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if cpu_millicores is not UNSET:
            field_dict["cpu_millicores"] = cpu_millicores
        if disk_mib is not UNSET:
            field_dict["disk_mib"] = disk_mib
        if memory_mib is not UNSET:
            field_dict["memory_mib"] = memory_mib
        if sleep_after_idle_seconds is not UNSET:
            field_dict["sleep_after_idle_seconds"] = sleep_after_idle_seconds

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_cpu_millicores(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        cpu_millicores = _parse_cpu_millicores(d.pop("cpu_millicores", UNSET))


        def _parse_disk_mib(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        disk_mib = _parse_disk_mib(d.pop("disk_mib", UNSET))


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


        resize_payload = cls(
            cpu_millicores=cpu_millicores,
            disk_mib=disk_mib,
            memory_mib=memory_mib,
            sleep_after_idle_seconds=sleep_after_idle_seconds,
        )

        return resize_payload

