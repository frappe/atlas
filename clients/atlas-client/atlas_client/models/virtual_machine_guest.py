from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.virtual_machine_guest_metadata import VirtualMachineGuestMetadata





T = TypeVar("T", bound="VirtualMachineGuest")



@_attrs_define
class VirtualMachineGuest:
    """ The guest configuration of one virtual machine.

        Attributes:
            hostname (None | str):
            metadata (VirtualMachineGuestMetadata):
            ssh_keys (list[str]):
     """

    hostname: None | str
    metadata: VirtualMachineGuestMetadata
    ssh_keys: list[str]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.virtual_machine_guest_metadata import VirtualMachineGuestMetadata # noqa: PLC0415
        hostname: None | str
        hostname = self.hostname

        metadata = self.metadata.to_dict()

        ssh_keys = self.ssh_keys




        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "hostname": hostname,
            "metadata": metadata,
            "ssh_keys": ssh_keys,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.virtual_machine_guest_metadata import VirtualMachineGuestMetadata # noqa: PLC0415
        d = dict(src_dict)
        def _parse_hostname(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        hostname = _parse_hostname(d.pop("hostname"))


        metadata = VirtualMachineGuestMetadata.from_dict(d.pop("metadata"))




        ssh_keys = cast(list[str], d.pop("ssh_keys"))


        virtual_machine_guest = cls(
            hostname=hostname,
            metadata=metadata,
            ssh_keys=ssh_keys,
        )


        virtual_machine_guest.additional_properties = d
        return virtual_machine_guest

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
