from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.host_response import HostResponse





T = TypeVar("T", bound="PageHostResponse")



@_attrs_define
class PageHostResponse:
    """ 
        Attributes:
            has_more (bool): Whether another page follows this page.
            items (list[HostResponse]): Resources in this page.
            limit (int): Requested maximum page size.
            offset (int): Number of matching resources skipped before this page.
     """

    has_more: bool
    items: list[HostResponse]
    limit: int
    offset: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.host_response import HostResponse # noqa: PLC0415
        has_more = self.has_more

        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)



        limit = self.limit

        offset = self.offset


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "has_more": has_more,
            "items": items,
            "limit": limit,
            "offset": offset,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.host_response import HostResponse # noqa: PLC0415
        d = dict(src_dict)
        has_more = d.pop("has_more")

        items = []
        _items = d.pop("items")
        for items_item_data in (_items):
            items_item = HostResponse.from_dict(items_item_data)



            items.append(items_item)


        limit = d.pop("limit")

        offset = d.pop("offset")

        page_host_response = cls(
            has_more=has_more,
            items=items,
            limit=limit,
            offset=offset,
        )


        page_host_response.additional_properties = d
        return page_host_response

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
