from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="BeaconResource")



@_attrs_define
class BeaconResource:
    """ One regional resource that Beacon, the incident manager, monitors.

        Attributes:
            parent_id (str): Resource ID of the parent, or empty for a root resource.
            resource_id (str): Atlas ID. Telemetry from this resource carries it.
            resource_type (str): `Atlas`, `Host`, `Server`, a service node DocType, or `Cargo Server`.
            status (str): `Pending`, `Active`, `Suspended` or `Terminated`.
            title (str): Name that people see.
     """

    parent_id: str
    resource_id: str
    resource_type: str
    status: str
    title: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        parent_id = self.parent_id

        resource_id = self.resource_id

        resource_type = self.resource_type

        status = self.status

        title = self.title


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "parent_id": parent_id,
            "resource_id": resource_id,
            "resource_type": resource_type,
            "status": status,
            "title": title,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        parent_id = d.pop("parent_id")

        resource_id = d.pop("resource_id")

        resource_type = d.pop("resource_type")

        status = d.pop("status")

        title = d.pop("title")

        beacon_resource = cls(
            parent_id=parent_id,
            resource_id=resource_id,
            resource_type=resource_type,
            status=status,
            title=title,
        )


        beacon_resource.additional_properties = d
        return beacon_resource

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
