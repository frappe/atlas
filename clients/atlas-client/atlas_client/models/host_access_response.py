from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
import datetime






T = TypeVar("T", bound="HostAccessResponse")



@_attrs_define
class HostAccessResponse:
    """ One active access grant.

        Attributes:
            email (str): Email of the person, in lowercase.
            expires_at (datetime.datetime): End of the access.
            host_id (str): Metal Server ID, or `all`.
     """

    email: str
    expires_at: datetime.datetime
    host_id: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        email = self.email

        expires_at = self.expires_at.isoformat()

        host_id = self.host_id


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "email": email,
            "expires_at": expires_at,
            "host_id": host_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        email = d.pop("email")

        expires_at = datetime.datetime.fromisoformat(d.pop("expires_at"))




        host_id = d.pop("host_id")

        host_access_response = cls(
            email=email,
            expires_at=expires_at,
            host_id=host_id,
        )


        host_access_response.additional_properties = d
        return host_access_response

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
