from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.virtual_machine_guest_filesystem import VirtualMachineGuestFilesystem
  from ..models.virtual_machine_guest_memory import VirtualMachineGuestMemory





T = TypeVar("T", bound="VirtualMachineGuestUsage")



@_attrs_define
class VirtualMachineGuestUsage:
    """ Resource use that the guest agent reports from inside the VM. The defaults describe a guest that did not answer.

        Attributes:
            load_average (list[float] | Unset): Load average over 1, 5 and 15 minutes.
            memory (VirtualMachineGuestMemory | Unset): Memory use that the guest kernel reports in `/proc/meminfo`.
            root_filesystem (VirtualMachineGuestFilesystem | Unset): Size and use of the guest root file system.
            system_state (str | Unset): Output of `systemctl is-system-running`, such as `running` or `degraded`.
                `unreachable` when the guest agent did not answer. Default: 'unreachable'.
     """

    load_average: list[float] | Unset = UNSET
    memory: VirtualMachineGuestMemory | Unset = UNSET
    root_filesystem: VirtualMachineGuestFilesystem | Unset = UNSET
    system_state: str | Unset = 'unreachable'
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.virtual_machine_guest_filesystem import VirtualMachineGuestFilesystem # noqa: PLC0415
        from ..models.virtual_machine_guest_memory import VirtualMachineGuestMemory # noqa: PLC0415
        load_average: list[float] | Unset = UNSET
        if not isinstance(self.load_average, Unset):
            load_average = self.load_average



        memory: dict[str, Any] | Unset = UNSET
        if not isinstance(self.memory, Unset):
            memory = self.memory.to_dict()

        root_filesystem: dict[str, Any] | Unset = UNSET
        if not isinstance(self.root_filesystem, Unset):
            root_filesystem = self.root_filesystem.to_dict()

        system_state = self.system_state


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
        })
        if load_average is not UNSET:
            field_dict["load_average"] = load_average
        if memory is not UNSET:
            field_dict["memory"] = memory
        if root_filesystem is not UNSET:
            field_dict["root_filesystem"] = root_filesystem
        if system_state is not UNSET:
            field_dict["system_state"] = system_state

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.virtual_machine_guest_filesystem import VirtualMachineGuestFilesystem # noqa: PLC0415
        from ..models.virtual_machine_guest_memory import VirtualMachineGuestMemory # noqa: PLC0415
        d = dict(src_dict)
        load_average = cast(list[float], d.pop("load_average", UNSET))


        _memory = d.pop("memory", UNSET)
        memory: VirtualMachineGuestMemory | Unset
        if isinstance(_memory,  Unset):
            memory = UNSET
        else:
            memory = VirtualMachineGuestMemory.from_dict(_memory)




        _root_filesystem = d.pop("root_filesystem", UNSET)
        root_filesystem: VirtualMachineGuestFilesystem | Unset
        if isinstance(_root_filesystem,  Unset):
            root_filesystem = UNSET
        else:
            root_filesystem = VirtualMachineGuestFilesystem.from_dict(_root_filesystem)




        system_state = d.pop("system_state", UNSET)

        virtual_machine_guest_usage = cls(
            load_average=load_average,
            memory=memory,
            root_filesystem=root_filesystem,
            system_state=system_state,
        )


        virtual_machine_guest_usage.additional_properties = d
        return virtual_machine_guest_usage

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
