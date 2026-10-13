from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.rescue_payload_access import RescuePayloadAccess
from ..models.rescue_payload_action import RescuePayloadAction
from ..types import UNSET, Unset






T = TypeVar("T", bound="RescuePayload")



@_attrs_define
class RescuePayload:
    """ Select rescue mode. Atlas chooses the image for each new session.

        Attributes:
            enabled (bool): Enter rescue when true; exit when false.
            access (RescuePayloadAccess | Unset):  Default: RescuePayloadAccess.PROTECTED.
            action (RescuePayloadAction | Unset):  Default: RescuePayloadAction.VALUE_0.
            allow_unprotected (bool | Unset): Explicit acknowledgement of writes without undo. Default: False.
            recovery_generation (int | Unset): Checkpoint session shown to the user; required for rollback and finish.
                Default: 0.
     """

    enabled: bool
    access: RescuePayloadAccess | Unset = RescuePayloadAccess.PROTECTED
    action: RescuePayloadAction | Unset = RescuePayloadAction.VALUE_0
    allow_unprotected: bool | Unset = False
    recovery_generation: int | Unset = 0





    def to_dict(self) -> dict[str, Any]:
        enabled = self.enabled

        access: str | Unset = UNSET
        if not isinstance(self.access, Unset):
            access = self.access.value


        action: str | Unset = UNSET
        if not isinstance(self.action, Unset):
            action = self.action.value


        allow_unprotected = self.allow_unprotected

        recovery_generation = self.recovery_generation


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "enabled": enabled,
        })
        if access is not UNSET:
            field_dict["access"] = access
        if action is not UNSET:
            field_dict["action"] = action
        if allow_unprotected is not UNSET:
            field_dict["allow_unprotected"] = allow_unprotected
        if recovery_generation is not UNSET:
            field_dict["recovery_generation"] = recovery_generation

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        enabled = d.pop("enabled")

        _access = d.pop("access", UNSET)
        access: RescuePayloadAccess | Unset
        if isinstance(_access,  Unset):
            access = UNSET
        else:
            access = RescuePayloadAccess(_access)




        _action = d.pop("action", UNSET)
        action: RescuePayloadAction | Unset
        if isinstance(_action,  Unset):
            action = UNSET
        else:
            action = RescuePayloadAction(_action)




        allow_unprotected = d.pop("allow_unprotected", UNSET)

        recovery_generation = d.pop("recovery_generation", UNSET)

        rescue_payload = cls(
            enabled=enabled,
            access=access,
            action=action,
            allow_unprotected=allow_unprotected,
            recovery_generation=recovery_generation,
        )

        return rescue_payload

