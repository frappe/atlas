from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="RescuePayload")



@_attrs_define
class RescuePayload:
    """ Select rescue mode. Atlas chooses the image for each new session.

        Attributes:
            enabled (bool): Enter rescue when true; exit when false.
     """

    enabled: bool





    def to_dict(self) -> dict[str, Any]:
        enabled = self.enabled


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "enabled": enabled,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        enabled = d.pop("enabled")

        rescue_payload = cls(
            enabled=enabled,
        )

        return rescue_payload

