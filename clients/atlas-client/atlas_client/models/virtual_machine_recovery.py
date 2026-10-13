from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="VirtualMachineRecovery")



@_attrs_define
class VirtualMachineRecovery:
    """ Checkpoint protection retained independently of rescue boot.

        Attributes:
            action (str):
            active (bool):
            checkpoint (bool):
            complete (bool):
            expires_at (str):
            generation (int):
            rollback_applied (bool):
     """

    action: str
    active: bool
    checkpoint: bool
    complete: bool
    expires_at: str
    generation: int
    rollback_applied: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        action = self.action

        active = self.active

        checkpoint = self.checkpoint

        complete = self.complete

        expires_at = self.expires_at

        generation = self.generation

        rollback_applied = self.rollback_applied


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "action": action,
            "active": active,
            "checkpoint": checkpoint,
            "complete": complete,
            "expires_at": expires_at,
            "generation": generation,
            "rollback_applied": rollback_applied,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        action = d.pop("action")

        active = d.pop("active")

        checkpoint = d.pop("checkpoint")

        complete = d.pop("complete")

        expires_at = d.pop("expires_at")

        generation = d.pop("generation")

        rollback_applied = d.pop("rollback_applied")

        virtual_machine_recovery = cls(
            action=action,
            active=active,
            checkpoint=checkpoint,
            complete=complete,
            expires_at=expires_at,
            generation=generation,
            rollback_applied=rollback_applied,
        )


        virtual_machine_recovery.additional_properties = d
        return virtual_machine_recovery

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
