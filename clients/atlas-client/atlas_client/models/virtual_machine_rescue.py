from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="VirtualMachineRescue")



@_attrs_define
class VirtualMachineRescue:
    """ Requested and applied rescue mode from the assigned host.

        Attributes:
            enabled (bool): Requested rescue mode.
            image_ref (None | str): Image pinned to this rescue session, or null.
            observed_enabled (bool): Last successfully applied rescue mode.
            pending (bool): Whether the host has an unapplied rescue selection.
     """

    enabled: bool
    image_ref: None | str
    observed_enabled: bool
    pending: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        enabled = self.enabled

        image_ref: None | str
        image_ref = self.image_ref

        observed_enabled = self.observed_enabled

        pending = self.pending


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "enabled": enabled,
            "image_ref": image_ref,
            "observed_enabled": observed_enabled,
            "pending": pending,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        enabled = d.pop("enabled")

        def _parse_image_ref(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        image_ref = _parse_image_ref(d.pop("image_ref"))


        observed_enabled = d.pop("observed_enabled")

        pending = d.pop("pending")

        virtual_machine_rescue = cls(
            enabled=enabled,
            image_ref=image_ref,
            observed_enabled=observed_enabled,
            pending=pending,
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
