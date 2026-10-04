from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="WarpgateSessionsClosePayload")



@_attrs_define
class WarpgateSessionsClosePayload:
    """ End every live Warpgate session of one person now.

        Attributes:
            email (str): Email that the person signs in to Central with.
     """

    email: str





    def to_dict(self) -> dict[str, Any]:
        email = self.email


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "email": email,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        email = d.pop("email")

        warpgate_sessions_close_payload = cls(
            email=email,
        )

        return warpgate_sessions_close_payload

