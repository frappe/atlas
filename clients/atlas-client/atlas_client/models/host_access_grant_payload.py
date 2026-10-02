from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
import datetime






T = TypeVar("T", bound="HostAccessGrantPayload")



@_attrs_define
class HostAccessGrantPayload:
    """ Open one host, or every host, to one person until a time.

        Attributes:
            email (str): Email that the person signs in to Central with.
            expires_at (datetime.datetime): End of the access, with a time zone. At most 24 hours away by default.
     """

    email: str
    expires_at: datetime.datetime





    def to_dict(self) -> dict[str, Any]:
        email = self.email

        expires_at = self.expires_at.isoformat()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "email": email,
            "expires_at": expires_at,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        email = d.pop("email")

        expires_at = datetime.datetime.fromisoformat(d.pop("expires_at"))




        host_access_grant_payload = cls(
            email=email,
            expires_at=expires_at,
        )

        return host_access_grant_payload

