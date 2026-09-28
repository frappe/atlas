from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.virtual_machine_detail_response_architecture import VirtualMachineDetailResponseArchitecture
from typing import cast

if TYPE_CHECKING:
  from ..models.public_ip_response import PublicIPResponse
  from ..models.virtual_machine_compute import VirtualMachineCompute
  from ..models.virtual_machine_detail_response_tags import VirtualMachineDetailResponseTags
  from ..models.virtual_machine_disk import VirtualMachineDisk
  from ..models.virtual_machine_guest import VirtualMachineGuest
  from ..models.virtual_machine_network import VirtualMachineNetwork





T = TypeVar("T", bound="VirtualMachineDetailResponse")



@_attrs_define
class VirtualMachineDetailResponse:
    """ One virtual machine with its state, addresses, and guest configuration.

        Attributes:
            architecture (VirtualMachineDetailResponseArchitecture): CPU architecture.
            compute (VirtualMachineCompute): The compute shape of one virtual machine.
            created_at (int): Creation time as Unix seconds.
            current_state (str): Current state reported by the host or managed by Atlas.
            desired_state (None | str): State requested from the host, or null when unavailable.
            disk (VirtualMachineDisk): The disk size and its rate limits.
            disk_encryption (bool): Whether the guest encrypts its root disk.
            error (None | str): Current host-reported error, or null.
            guest (VirtualMachineGuest): The guest configuration of one virtual machine.
            id (str): Virtual machine ID.
            image_id (str): Image used to create the virtual machine.
            is_privileged (bool): Whether the guest can reach every tenant through the mesh.
            network (VirtualMachineNetwork): The addresses, internet access, and network limits of one virtual machine.
            public_ipv4 (None | PublicIPResponse): Attached public IPv4 allocation, or null.
            public_ipv6 (None | PublicIPResponse): Attached public IPv6 allocation, or null.
            tags (VirtualMachineDetailResponseTags): Resource tags as key-value pairs.
            tenant_id (int): Tenant that owns the virtual machine.
     """

    architecture: VirtualMachineDetailResponseArchitecture
    compute: VirtualMachineCompute
    created_at: int
    current_state: str
    desired_state: None | str
    disk: VirtualMachineDisk
    disk_encryption: bool
    error: None | str
    guest: VirtualMachineGuest
    id: str
    image_id: str
    is_privileged: bool
    network: VirtualMachineNetwork
    public_ipv4: None | PublicIPResponse
    public_ipv6: None | PublicIPResponse
    tags: VirtualMachineDetailResponseTags
    tenant_id: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.public_ip_response import PublicIPResponse # noqa: PLC0415
        from ..models.virtual_machine_compute import VirtualMachineCompute # noqa: PLC0415
        from ..models.virtual_machine_detail_response_tags import VirtualMachineDetailResponseTags # noqa: PLC0415
        from ..models.virtual_machine_disk import VirtualMachineDisk # noqa: PLC0415
        from ..models.virtual_machine_guest import VirtualMachineGuest # noqa: PLC0415
        from ..models.virtual_machine_network import VirtualMachineNetwork # noqa: PLC0415
        architecture = self.architecture.value

        compute = self.compute.to_dict()

        created_at = self.created_at

        current_state = self.current_state

        desired_state: None | str
        desired_state = self.desired_state

        disk = self.disk.to_dict()

        disk_encryption = self.disk_encryption

        error: None | str
        error = self.error

        guest = self.guest.to_dict()

        id = self.id

        image_id = self.image_id

        is_privileged = self.is_privileged

        network = self.network.to_dict()

        public_ipv4: dict[str, Any] | None
        if isinstance(self.public_ipv4, PublicIPResponse):
            public_ipv4 = self.public_ipv4.to_dict()
        else:
            public_ipv4 = self.public_ipv4

        public_ipv6: dict[str, Any] | None
        if isinstance(self.public_ipv6, PublicIPResponse):
            public_ipv6 = self.public_ipv6.to_dict()
        else:
            public_ipv6 = self.public_ipv6

        tags = self.tags.to_dict()

        tenant_id = self.tenant_id


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "architecture": architecture,
            "compute": compute,
            "created_at": created_at,
            "current_state": current_state,
            "desired_state": desired_state,
            "disk": disk,
            "disk_encryption": disk_encryption,
            "error": error,
            "guest": guest,
            "id": id,
            "image_id": image_id,
            "is_privileged": is_privileged,
            "network": network,
            "public_ipv4": public_ipv4,
            "public_ipv6": public_ipv6,
            "tags": tags,
            "tenant_id": tenant_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.public_ip_response import PublicIPResponse # noqa: PLC0415
        from ..models.virtual_machine_compute import VirtualMachineCompute # noqa: PLC0415
        from ..models.virtual_machine_detail_response_tags import VirtualMachineDetailResponseTags # noqa: PLC0415
        from ..models.virtual_machine_disk import VirtualMachineDisk # noqa: PLC0415
        from ..models.virtual_machine_guest import VirtualMachineGuest # noqa: PLC0415
        from ..models.virtual_machine_network import VirtualMachineNetwork # noqa: PLC0415
        d = dict(src_dict)
        architecture = VirtualMachineDetailResponseArchitecture(d.pop("architecture"))




        compute = VirtualMachineCompute.from_dict(d.pop("compute"))




        created_at = d.pop("created_at")

        current_state = d.pop("current_state")

        def _parse_desired_state(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        desired_state = _parse_desired_state(d.pop("desired_state"))


        disk = VirtualMachineDisk.from_dict(d.pop("disk"))




        disk_encryption = d.pop("disk_encryption")

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




        def _parse_public_ipv4(data: object) -> None | PublicIPResponse:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                public_ipv4_type_0 = PublicIPResponse.from_dict(data)



                return public_ipv4_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PublicIPResponse, data)

        public_ipv4 = _parse_public_ipv4(d.pop("public_ipv4"))


        def _parse_public_ipv6(data: object) -> None | PublicIPResponse:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                public_ipv6_type_0 = PublicIPResponse.from_dict(data)



                return public_ipv6_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PublicIPResponse, data)

        public_ipv6 = _parse_public_ipv6(d.pop("public_ipv6"))


        tags = VirtualMachineDetailResponseTags.from_dict(d.pop("tags"))




        tenant_id = d.pop("tenant_id")

        virtual_machine_detail_response = cls(
            architecture=architecture,
            compute=compute,
            created_at=created_at,
            current_state=current_state,
            desired_state=desired_state,
            disk=disk,
            disk_encryption=disk_encryption,
            error=error,
            guest=guest,
            id=id,
            image_id=image_id,
            is_privileged=is_privileged,
            network=network,
            public_ipv4=public_ipv4,
            public_ipv6=public_ipv6,
            tags=tags,
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
