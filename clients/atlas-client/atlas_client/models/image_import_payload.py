from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.image_import_payload_image_type import ImageImportPayloadImageType
from ..types import UNSET, Unset






T = TypeVar("T", bound="ImageImportPayload")



@_attrs_define
class ImageImportPayload:
    """ The OCI image to import.

        Attributes:
            reference (str): OCI image reference, such as `nginx:1.29-alpine`.
            image_type (ImageImportPayloadImageType | Unset): Image visibility. Only tenant 0 can set `system`. Default:
                ImageImportPayloadImageType.MACHINE.
            title (str | Unset): Display title. The default is the reference. Default: ''.
     """

    reference: str
    image_type: ImageImportPayloadImageType | Unset = ImageImportPayloadImageType.MACHINE
    title: str | Unset = ''





    def to_dict(self) -> dict[str, Any]:
        reference = self.reference

        image_type: str | Unset = UNSET
        if not isinstance(self.image_type, Unset):
            image_type = self.image_type.value


        title = self.title


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "reference": reference,
        })
        if image_type is not UNSET:
            field_dict["image_type"] = image_type
        if title is not UNSET:
            field_dict["title"] = title

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        reference = d.pop("reference")

        _image_type = d.pop("image_type", UNSET)
        image_type: ImageImportPayloadImageType | Unset
        if isinstance(_image_type,  Unset):
            image_type = UNSET
        else:
            image_type = ImageImportPayloadImageType(_image_type)




        title = d.pop("title", UNSET)

        image_import_payload = cls(
            reference=reference,
            image_type=image_type,
            title=title,
        )

        return image_import_payload

