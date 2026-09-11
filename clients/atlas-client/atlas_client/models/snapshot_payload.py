from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.snapshot_payload_image_type import SnapshotPayloadImageType
from ..types import UNSET, Unset






T = TypeVar("T", bound="SnapshotPayload")



@_attrs_define
class SnapshotPayload:
    """ Values that create one image from a virtual machine.

        Attributes:
            title (str):
            cache_image (bool | Unset):  Default: False.
            image_type (SnapshotPayloadImageType | Unset):  Default: SnapshotPayloadImageType.MACHINE.
            memory_snapshot (bool | Unset):  Default: False.
     """

    title: str
    cache_image: bool | Unset = False
    image_type: SnapshotPayloadImageType | Unset = SnapshotPayloadImageType.MACHINE
    memory_snapshot: bool | Unset = False





    def to_dict(self) -> dict[str, Any]:
        title = self.title

        cache_image = self.cache_image

        image_type: str | Unset = UNSET
        if not isinstance(self.image_type, Unset):
            image_type = self.image_type.value


        memory_snapshot = self.memory_snapshot


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "title": title,
        })
        if cache_image is not UNSET:
            field_dict["cache_image"] = cache_image
        if image_type is not UNSET:
            field_dict["image_type"] = image_type
        if memory_snapshot is not UNSET:
            field_dict["memory_snapshot"] = memory_snapshot

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        title = d.pop("title")

        cache_image = d.pop("cache_image", UNSET)

        _image_type = d.pop("image_type", UNSET)
        image_type: SnapshotPayloadImageType | Unset
        if isinstance(_image_type,  Unset):
            image_type = UNSET
        else:
            image_type = SnapshotPayloadImageType(_image_type)




        memory_snapshot = d.pop("memory_snapshot", UNSET)

        snapshot_payload = cls(
            title=title,
            cache_image=cache_image,
            image_type=image_type,
            memory_snapshot=memory_snapshot,
        )

        return snapshot_payload

