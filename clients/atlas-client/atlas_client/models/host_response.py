from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.host_response_tags import HostResponseTags





T = TypeVar("T", bound="HostResponse")



@_attrs_define
class HostResponse:
    """ One Metal host that people can be granted SSH access to.

        Attributes:
            id (str): Metal Server ID. Use it in the access routes.
            status (str): Host lifecycle state.
            tags (HostResponseTags): Host tags as key-value pairs.
            title (str): Host name. People type it in `ssh <email>:<title>@warpgate.<domain>`.
     """

    id: str
    status: str
    tags: HostResponseTags
    title: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.host_response_tags import HostResponseTags # noqa: PLC0415
        id = self.id

        status = self.status

        tags = self.tags.to_dict()

        title = self.title


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "id": id,
            "status": status,
            "tags": tags,
            "title": title,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.host_response_tags import HostResponseTags # noqa: PLC0415
        d = dict(src_dict)
        id = d.pop("id")

        status = d.pop("status")

        tags = HostResponseTags.from_dict(d.pop("tags"))




        title = d.pop("title")

        host_response = cls(
            id=id,
            status=status,
            tags=tags,
            title=title,
        )


        host_response.additional_properties = d
        return host_response

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
