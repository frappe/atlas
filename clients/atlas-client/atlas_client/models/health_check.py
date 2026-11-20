from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset






T = TypeVar("T", bound="HealthCheck")



@_attrs_define
class HealthCheck:
    """ An HTTPS check on port 443 that asks `host` and `path` at one address.

        Attributes:
            host (str):
            path (str | Unset):  Default: '/'.
     """

    host: str
    path: str | Unset = '/'





    def to_dict(self) -> dict[str, Any]:
        host = self.host

        path = self.path


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "host": host,
        })
        if path is not UNSET:
            field_dict["path"] = path

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        host = d.pop("host")

        path = d.pop("path", UNSET)

        health_check = cls(
            host=host,
            path=path,
        )

        return health_check

