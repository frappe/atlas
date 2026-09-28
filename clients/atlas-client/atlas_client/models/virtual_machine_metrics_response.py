from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.virtual_machine_compute_usage import VirtualMachineComputeUsage
  from ..models.virtual_machine_disk_usage import VirtualMachineDiskUsage
  from ..models.virtual_machine_network_usage import VirtualMachineNetworkUsage





T = TypeVar("T", bound="VirtualMachineMetricsResponse")



@_attrs_define
class VirtualMachineMetricsResponse:
    """ The current resource use of one virtual machine.

    A virtual machine that is neither running nor paused reports zero CPU and
    memory. Disk use and any surviving network counters remain available.

        Attributes:
            compute (VirtualMachineComputeUsage): Cumulative CPU time and current memory use.
            disk (VirtualMachineDiskUsage): The disk's requested size and its use as of the last reconcile pass.
            id (str): Virtual machine ID.
            network (VirtualMachineNetworkUsage): Cumulative unicast IP traffic for the lifetime of the traffic attachment.

                Counters survive guest stops while the attachment remains. Recreating the
                attachment or restarting Metal resets the counters.
     """

    compute: VirtualMachineComputeUsage
    disk: VirtualMachineDiskUsage
    id: str
    network: VirtualMachineNetworkUsage
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.virtual_machine_compute_usage import VirtualMachineComputeUsage # noqa: PLC0415
        from ..models.virtual_machine_disk_usage import VirtualMachineDiskUsage # noqa: PLC0415
        from ..models.virtual_machine_network_usage import VirtualMachineNetworkUsage # noqa: PLC0415
        compute = self.compute.to_dict()

        disk = self.disk.to_dict()

        id = self.id

        network = self.network.to_dict()


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "compute": compute,
            "disk": disk,
            "id": id,
            "network": network,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.virtual_machine_compute_usage import VirtualMachineComputeUsage # noqa: PLC0415
        from ..models.virtual_machine_disk_usage import VirtualMachineDiskUsage # noqa: PLC0415
        from ..models.virtual_machine_network_usage import VirtualMachineNetworkUsage # noqa: PLC0415
        d = dict(src_dict)
        compute = VirtualMachineComputeUsage.from_dict(d.pop("compute"))




        disk = VirtualMachineDiskUsage.from_dict(d.pop("disk"))




        id = d.pop("id")

        network = VirtualMachineNetworkUsage.from_dict(d.pop("network"))




        virtual_machine_metrics_response = cls(
            compute=compute,
            disk=disk,
            id=id,
            network=network,
        )


        virtual_machine_metrics_response.additional_properties = d
        return virtual_machine_metrics_response

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
