from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.virtual_machine_compute_usage import VirtualMachineComputeUsage
  from ..models.virtual_machine_disk_usage import VirtualMachineDiskUsage
  from ..models.virtual_machine_guest_usage import VirtualMachineGuestUsage
  from ..models.virtual_machine_network_usage import VirtualMachineNetworkUsage





T = TypeVar("T", bound="VirtualMachineMetricsSample")



@_attrs_define
class VirtualMachineMetricsSample:
    """ 
        Attributes:
            compute (VirtualMachineComputeUsage): Cumulative CPU time and current memory use.
            disk (VirtualMachineDiskUsage): The disk's size, configured limits, and sampled I/O rates.
            network (VirtualMachineNetworkUsage): Cumulative unicast IP traffic for the lifetime of the traffic attachment.

                Counters survive guest stops while the attachment remains. Recreating the
                attachment or restarting Metal resets the counters.
            timestamp (int): UTC Unix timestamp in seconds.
            up (bool):
            guest (VirtualMachineGuestUsage | Unset): Resource use that the guest agent reports from inside the VM. The
                defaults describe a guest that did not answer.
     """

    compute: VirtualMachineComputeUsage
    disk: VirtualMachineDiskUsage
    network: VirtualMachineNetworkUsage
    timestamp: int
    up: bool
    guest: VirtualMachineGuestUsage | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.virtual_machine_compute_usage import VirtualMachineComputeUsage # noqa: PLC0415
        from ..models.virtual_machine_disk_usage import VirtualMachineDiskUsage # noqa: PLC0415
        from ..models.virtual_machine_guest_usage import VirtualMachineGuestUsage # noqa: PLC0415
        from ..models.virtual_machine_network_usage import VirtualMachineNetworkUsage # noqa: PLC0415
        compute = self.compute.to_dict()

        disk = self.disk.to_dict()

        network = self.network.to_dict()

        timestamp = self.timestamp

        up = self.up

        guest: dict[str, Any] | Unset = UNSET
        if not isinstance(self.guest, Unset):
            guest = self.guest.to_dict()


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "compute": compute,
            "disk": disk,
            "network": network,
            "timestamp": timestamp,
            "up": up,
        })
        if guest is not UNSET:
            field_dict["guest"] = guest

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.virtual_machine_compute_usage import VirtualMachineComputeUsage # noqa: PLC0415
        from ..models.virtual_machine_disk_usage import VirtualMachineDiskUsage # noqa: PLC0415
        from ..models.virtual_machine_guest_usage import VirtualMachineGuestUsage # noqa: PLC0415
        from ..models.virtual_machine_network_usage import VirtualMachineNetworkUsage # noqa: PLC0415
        d = dict(src_dict)
        compute = VirtualMachineComputeUsage.from_dict(d.pop("compute"))




        disk = VirtualMachineDiskUsage.from_dict(d.pop("disk"))




        network = VirtualMachineNetworkUsage.from_dict(d.pop("network"))




        timestamp = d.pop("timestamp")

        up = d.pop("up")

        _guest = d.pop("guest", UNSET)
        guest: VirtualMachineGuestUsage | Unset
        if isinstance(_guest,  Unset):
            guest = UNSET
        else:
            guest = VirtualMachineGuestUsage.from_dict(_guest)




        virtual_machine_metrics_sample = cls(
            compute=compute,
            disk=disk,
            network=network,
            timestamp=timestamp,
            up=up,
            guest=guest,
        )


        virtual_machine_metrics_sample.additional_properties = d
        return virtual_machine_metrics_sample

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
