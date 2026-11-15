from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.virtual_machine_response_architecture import VirtualMachineResponseArchitecture
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.public_ip_response import PublicIPResponse
  from ..models.virtual_machine_response_tags import VirtualMachineResponseTags





T = TypeVar("T", bound="VirtualMachineResponse")



@_attrs_define
class VirtualMachineResponse:
    """ A stored tenant virtual machine.

        Attributes:
            architecture (VirtualMachineResponseArchitecture): CPU architecture.
            cpu_millicores (int): CPU capacity in millicores.
            created_at (int): Creation time as Unix seconds.
            disk_mib (int): Root disk capacity in MiB.
            id (str): Virtual machine ID.
            image_id (str): Image used to create the virtual machine.
            is_termination_protected (bool): Whether deletion is blocked.
            memory_mib (int): Memory capacity in MiB.
            public_ipv4 (None | PublicIPResponse): Attached public IPv4 allocation, or null.
            public_ipv6 (None | PublicIPResponse): Attached public IPv6 allocation, or null.
            sleep_after_idle_seconds (int): Idle time before automatic stop. Zero disables it.
            tags (VirtualMachineResponseTags): Resource tags as key-value pairs.
            tenant_id (int): Tenant that owns the virtual machine.
            baseline_cpu_millicores (int | Unset): CPU a burstable VM can always use. Zero when not burstable. Default: 0.
            coma_after_idle_seconds (int | Unset): Idle time before disk offload. Zero disables it. Default: 0.
            deep_sleep_after_idle_seconds (int | Unset): Idle time before memory offload. Zero disables it. Default: 0.
            is_burstable (bool | Unset): Whether the VM can use CPU above its baseline. Default: False.
            sliding_window_minutes (int | Unset): How far back average CPU use is measured. Zero when not burstable.
                Default: 0.
     """

    architecture: VirtualMachineResponseArchitecture
    cpu_millicores: int
    created_at: int
    disk_mib: int
    id: str
    image_id: str
    is_termination_protected: bool
    memory_mib: int
    public_ipv4: None | PublicIPResponse
    public_ipv6: None | PublicIPResponse
    sleep_after_idle_seconds: int
    tags: VirtualMachineResponseTags
    tenant_id: int
    baseline_cpu_millicores: int | Unset = 0
    coma_after_idle_seconds: int | Unset = 0
    deep_sleep_after_idle_seconds: int | Unset = 0
    is_burstable: bool | Unset = False
    sliding_window_minutes: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.public_ip_response import PublicIPResponse # noqa: PLC0415
        from ..models.virtual_machine_response_tags import VirtualMachineResponseTags # noqa: PLC0415
        architecture = self.architecture.value

        cpu_millicores = self.cpu_millicores

        created_at = self.created_at

        disk_mib = self.disk_mib

        id = self.id

        image_id = self.image_id

        is_termination_protected = self.is_termination_protected

        memory_mib = self.memory_mib

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

        sleep_after_idle_seconds = self.sleep_after_idle_seconds

        tags = self.tags.to_dict()

        tenant_id = self.tenant_id

        baseline_cpu_millicores = self.baseline_cpu_millicores

        coma_after_idle_seconds = self.coma_after_idle_seconds

        deep_sleep_after_idle_seconds = self.deep_sleep_after_idle_seconds

        is_burstable = self.is_burstable

        sliding_window_minutes = self.sliding_window_minutes


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "architecture": architecture,
            "cpu_millicores": cpu_millicores,
            "created_at": created_at,
            "disk_mib": disk_mib,
            "id": id,
            "image_id": image_id,
            "is_termination_protected": is_termination_protected,
            "memory_mib": memory_mib,
            "public_ipv4": public_ipv4,
            "public_ipv6": public_ipv6,
            "sleep_after_idle_seconds": sleep_after_idle_seconds,
            "tags": tags,
            "tenant_id": tenant_id,
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
        from ..models.public_ip_response import PublicIPResponse # noqa: PLC0415
        from ..models.virtual_machine_response_tags import VirtualMachineResponseTags # noqa: PLC0415
        d = dict(src_dict)
        architecture = VirtualMachineResponseArchitecture(d.pop("architecture"))




        cpu_millicores = d.pop("cpu_millicores")

        created_at = d.pop("created_at")

        disk_mib = d.pop("disk_mib")

        id = d.pop("id")

        image_id = d.pop("image_id")

        is_termination_protected = d.pop("is_termination_protected")

        memory_mib = d.pop("memory_mib")

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


        sleep_after_idle_seconds = d.pop("sleep_after_idle_seconds")

        tags = VirtualMachineResponseTags.from_dict(d.pop("tags"))




        tenant_id = d.pop("tenant_id")

        baseline_cpu_millicores = d.pop("baseline_cpu_millicores", UNSET)

        coma_after_idle_seconds = d.pop("coma_after_idle_seconds", UNSET)

        deep_sleep_after_idle_seconds = d.pop("deep_sleep_after_idle_seconds", UNSET)

        is_burstable = d.pop("is_burstable", UNSET)

        sliding_window_minutes = d.pop("sliding_window_minutes", UNSET)

        virtual_machine_response = cls(
            architecture=architecture,
            cpu_millicores=cpu_millicores,
            created_at=created_at,
            disk_mib=disk_mib,
            id=id,
            image_id=image_id,
            is_termination_protected=is_termination_protected,
            memory_mib=memory_mib,
            public_ipv4=public_ipv4,
            public_ipv6=public_ipv6,
            sleep_after_idle_seconds=sleep_after_idle_seconds,
            tags=tags,
            tenant_id=tenant_id,
            baseline_cpu_millicores=baseline_cpu_millicores,
            coma_after_idle_seconds=coma_after_idle_seconds,
            deep_sleep_after_idle_seconds=deep_sleep_after_idle_seconds,
            is_burstable=is_burstable,
            sliding_window_minutes=sliding_window_minutes,
        )


        virtual_machine_response.additional_properties = d
        return virtual_machine_response

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
