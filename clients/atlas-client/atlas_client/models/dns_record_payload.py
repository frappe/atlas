from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset






T = TypeVar("T", bound="DnsRecordPayload")



@_attrs_define
class DnsRecordPayload:
    """ The address of one regional service name.

        Attributes:
            address (str): Public address for a service name, WG Mesh address for its `internal-` name. Atlas sets an A or
                AAAA record.
            ttl (int | Unset): Record TTL in seconds. Default: 300.
     """

    address: str
    ttl: int | Unset = 300





    def to_dict(self) -> dict[str, Any]:
        address = self.address

        ttl = self.ttl


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "address": address,
        })
        if ttl is not UNSET:
            field_dict["ttl"] = ttl

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        address = d.pop("address")

        ttl = d.pop("ttl", UNSET)

        dns_record_payload = cls(
            address=address,
            ttl=ttl,
        )

        return dns_record_payload

