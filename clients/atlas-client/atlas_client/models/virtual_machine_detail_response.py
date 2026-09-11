from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.virtual_machine_compute import VirtualMachineCompute
  from ..models.virtual_machine_disk import VirtualMachineDisk
  from ..models.virtual_machine_guest import VirtualMachineGuest
  from ..models.virtual_machine_network import VirtualMachineNetwork





T = TypeVar("T", bound="VirtualMachineDetailResponse")



@_attrs_define
class VirtualMachineDetailResponse:
    """ One virtual machine with its state, addresses, and guest configuration.

        Attributes:
            compute (VirtualMachineCompute): The compute shape of one virtual machine.
            created_at (int):
            current_state (str):
            desired_state (None | str):
            disk (VirtualMachineDisk): The disk size and its rate limits.
            error (None | str):
            guest (VirtualMachineGuest): The guest configuration of one virtual machine.
            id (str):
            image_id (str):
            is_privileged (bool):
            network (VirtualMachineNetwork): The addresses and network limits of one virtual machine.
            tenant_id (int):
     """

    compute: VirtualMachineCompute
    created_at: int
    current_state: str
    desired_state: None | str
    disk: VirtualMachineDisk
    error: None | str
    guest: VirtualMachineGuest
    id: str
    image_id: str
    is_privileged: bool
    network: VirtualMachineNetwork
    tenant_id: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.virtual_machine_compute import VirtualMachineCompute # noqa: PLC0415
        from ..models.virtual_machine_disk import VirtualMachineDisk # noqa: PLC0415
        from ..models.virtual_machine_guest import VirtualMachineGuest # noqa: PLC0415
        from ..models.virtual_machine_network import VirtualMachineNetwork # noqa: PLC0415
        compute = self.compute.to_dict()

        created_at = self.created_at

        current_state = self.current_state

        desired_state: None | str
        desired_state = self.desired_state

        disk = self.disk.to_dict()

        error: None | str
        error = self.error

        guest = self.guest.to_dict()

        id = self.id

        image_id = self.image_id

        is_privileged = self.is_privileged

        network = self.network.to_dict()

        tenant_id = self.tenant_id


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "compute": compute,
            "created_at": created_at,
            "current_state": current_state,
            "desired_state": desired_state,
            "disk": disk,
            "error": error,
            "guest": guest,
            "id": id,
            "image_id": image_id,
            "is_privileged": is_privileged,
            "network": network,
            "tenant_id": tenant_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.virtual_machine_compute import VirtualMachineCompute # noqa: PLC0415
        from ..models.virtual_machine_disk import VirtualMachineDisk # noqa: PLC0415
        from ..models.virtual_machine_guest import VirtualMachineGuest # noqa: PLC0415
        from ..models.virtual_machine_network import VirtualMachineNetwork # noqa: PLC0415
        d = dict(src_dict)
        compute = VirtualMachineCompute.from_dict(d.pop("compute"))




        created_at = d.pop("created_at")

        current_state = d.pop("current_state")

        def _parse_desired_state(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        desired_state = _parse_desired_state(d.pop("desired_state"))


        disk = VirtualMachineDisk.from_dict(d.pop("disk"))




        def _parse_error(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        error = _parse_error(d.pop("error"))


        guest = VirtualMachineGuest.from_dict(d.pop("guest"))




        id = d.pop("id")

        image_id = d.pop("image_id")

        is_privileged = d.pop("is_privileged")

        network = VirtualMachineNetwork.from_dict(d.pop("network"))




        tenant_id = d.pop("tenant_id")

        virtual_machine_detail_response = cls(
            compute=compute,
            created_at=created_at,
            current_state=current_state,
            desired_state=desired_state,
            disk=disk,
            error=error,
            guest=guest,
            id=id,
            image_id=image_id,
            is_privileged=is_privileged,
            network=network,
            tenant_id=tenant_id,
        )


        virtual_machine_detail_response.additional_properties = d
        return virtual_machine_detail_response

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
