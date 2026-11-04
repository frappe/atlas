from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="DnsRecordPayload")



@_attrs_define
class DnsRecordPayload:
    """ The private address of one regional service name.

        Attributes:
            address (str): Private IPv4 or IPv6 address. Atlas sets an A or AAAA record.
     """

    address: str





    def to_dict(self) -> dict[str, Any]:
        address = self.address


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "address": address,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        address = d.pop("address")

        dns_record_payload = cls(
            address=address,
        )

        return dns_record_payload

