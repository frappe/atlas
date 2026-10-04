from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset






T = TypeVar("T", bound="VirtualMachineDiskUsage")



@_attrs_define
class VirtualMachineDiskUsage:
    """ The disk's size, configured limits, and sampled I/O rates.

        Attributes:
            size_mib (int): Requested disk size.
            used_mib (int): Disk use as of the last reconcile pass.
            iops_limit (int | Unset): Configured disk IOPS limit. Zero means unlimited. Default: 0.
            read_bytes_per_second (int | Unset): Average disk read throughput during the sample interval. Default: 0.
            read_milli_iops (int | Unset): Average disk read operations per second, in thousandths of an IOPS. Default: 0.
            throughput_limit_mibps (int | Unset): Configured disk throughput limit. Zero means unlimited. Default: 0.
            write_bytes_per_second (int | Unset): Average disk write throughput during the sample interval. Default: 0.
            write_milli_iops (int | Unset): Average disk write operations per second, in thousandths of an IOPS. Default: 0.
     """

    size_mib: int
    used_mib: int
    iops_limit: int | Unset = 0
    read_bytes_per_second: int | Unset = 0
    read_milli_iops: int | Unset = 0
    throughput_limit_mibps: int | Unset = 0
    write_bytes_per_second: int | Unset = 0
    write_milli_iops: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        size_mib = self.size_mib

        used_mib = self.used_mib

        iops_limit = self.iops_limit

        read_bytes_per_second = self.read_bytes_per_second

        read_milli_iops = self.read_milli_iops

        throughput_limit_mibps = self.throughput_limit_mibps

        write_bytes_per_second = self.write_bytes_per_second

        write_milli_iops = self.write_milli_iops


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "size_mib": size_mib,
            "used_mib": used_mib,
        })
        if iops_limit is not UNSET:
            field_dict["iops_limit"] = iops_limit
        if read_bytes_per_second is not UNSET:
            field_dict["read_bytes_per_second"] = read_bytes_per_second
        if read_milli_iops is not UNSET:
            field_dict["read_milli_iops"] = read_milli_iops
        if throughput_limit_mibps is not UNSET:
            field_dict["throughput_limit_mibps"] = throughput_limit_mibps
        if write_bytes_per_second is not UNSET:
            field_dict["write_bytes_per_second"] = write_bytes_per_second
        if write_milli_iops is not UNSET:
            field_dict["write_milli_iops"] = write_milli_iops

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        size_mib = d.pop("size_mib")

        used_mib = d.pop("used_mib")

        iops_limit = d.pop("iops_limit", UNSET)

        read_bytes_per_second = d.pop("read_bytes_per_second", UNSET)

        read_milli_iops = d.pop("read_milli_iops", UNSET)

        throughput_limit_mibps = d.pop("throughput_limit_mibps", UNSET)

        write_bytes_per_second = d.pop("write_bytes_per_second", UNSET)

        write_milli_iops = d.pop("write_milli_iops", UNSET)

        virtual_machine_disk_usage = cls(
            size_mib=size_mib,
            used_mib=used_mib,
            iops_limit=iops_limit,
            read_bytes_per_second=read_bytes_per_second,
            read_milli_iops=read_milli_iops,
            throughput_limit_mibps=throughput_limit_mibps,
            write_bytes_per_second=write_bytes_per_second,
            write_milli_iops=write_milli_iops,
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
