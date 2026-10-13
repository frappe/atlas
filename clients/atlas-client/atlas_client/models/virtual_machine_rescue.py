from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.virtual_machine_recovery import VirtualMachineRecovery





T = TypeVar("T", bound="VirtualMachineRescue")



@_attrs_define
class VirtualMachineRescue:
    """ Requested and applied rescue mode from the assigned host.

        Attributes:
            enabled (bool): Requested rescue mode.
            image_ref (None | str): Image pinned to this rescue session, or null.
            observed_enabled (bool): Last successfully applied rescue mode.
            pending (bool): Whether the host has an unapplied rescue selection or recovery action.
            access (str | Unset):  Default: ''.
            recovery (None | Unset | VirtualMachineRecovery):
     """

    enabled: bool
    image_ref: None | str
    observed_enabled: bool
    pending: bool
    access: str | Unset = ''
    recovery: None | Unset | VirtualMachineRecovery = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.virtual_machine_recovery import VirtualMachineRecovery # noqa: PLC0415
        enabled = self.enabled

        image_ref: None | str
        image_ref = self.image_ref

        observed_enabled = self.observed_enabled

        pending = self.pending

        access = self.access

        recovery: dict[str, Any] | None | Unset
        if isinstance(self.recovery, Unset):
            recovery = UNSET
        elif isinstance(self.recovery, VirtualMachineRecovery):
            recovery = self.recovery.to_dict()
        else:
            recovery = self.recovery


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "enabled": enabled,
            "image_ref": image_ref,
            "observed_enabled": observed_enabled,
            "pending": pending,
        })
        if access is not UNSET:
            field_dict["access"] = access
        if recovery is not UNSET:
            field_dict["recovery"] = recovery

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.virtual_machine_recovery import VirtualMachineRecovery # noqa: PLC0415
        d = dict(src_dict)
        enabled = d.pop("enabled")

        def _parse_image_ref(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        image_ref = _parse_image_ref(d.pop("image_ref"))


        observed_enabled = d.pop("observed_enabled")

        pending = d.pop("pending")

        access = d.pop("access", UNSET)

        def _parse_recovery(data: object) -> None | Unset | VirtualMachineRecovery:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                recovery_type_0 = VirtualMachineRecovery.from_dict(data)



                return recovery_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | VirtualMachineRecovery, data)

        recovery = _parse_recovery(d.pop("recovery", UNSET))


        virtual_machine_rescue = cls(
            enabled=enabled,
            image_ref=image_ref,
            observed_enabled=observed_enabled,
            pending=pending,
            access=access,
            recovery=recovery,
        )


        virtual_machine_rescue.additional_properties = d
        return virtual_machine_rescue

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
