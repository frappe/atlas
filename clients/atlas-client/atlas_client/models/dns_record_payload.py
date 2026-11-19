from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.dns_record_payload_type_type_0 import DnsRecordPayloadTypeType0
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="DnsRecordPayload")



@_attrs_define
class DnsRecordPayload:
    """ The address of one regional service name, or the values of a TXT or CNAME record on it or a name under it.

        Attributes:
            address (None | str | Unset): Public address for a service name, WG Mesh address for its `internal-` name.
                Required for A and AAAA.
            ttl (int | Unset): Record TTL in seconds. Default: 300.
            type_ (DnsRecordPayloadTypeType0 | None | Unset): Record type. Left out, Atlas picks A or AAAA from `address`.
            values (list[str] | None | Unset): Unquoted TXT values, or the one target host name of a CNAME. Required for TXT
                and CNAME.
     """

    address: None | str | Unset = UNSET
    ttl: int | Unset = 300
    type_: DnsRecordPayloadTypeType0 | None | Unset = UNSET
    values: list[str] | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        address: None | str | Unset
        if isinstance(self.address, Unset):
            address = UNSET
        else:
            address = self.address

        ttl = self.ttl

        type_: None | str | Unset
        if isinstance(self.type_, Unset):
            type_ = UNSET
        elif isinstance(self.type_, DnsRecordPayloadTypeType0):
            type_ = self.type_.value
        else:
            type_ = self.type_

        values: list[str] | None | Unset
        if isinstance(self.values, Unset):
            values = UNSET
        elif isinstance(self.values, list):
            values = self.values


        else:
            values = self.values


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if address is not UNSET:
            field_dict["address"] = address
        if ttl is not UNSET:
            field_dict["ttl"] = ttl
        if type_ is not UNSET:
            field_dict["type"] = type_
        if values is not UNSET:
            field_dict["values"] = values

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_address(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        address = _parse_address(d.pop("address", UNSET))


        ttl = d.pop("ttl", UNSET)

        def _parse_type_(data: object) -> DnsRecordPayloadTypeType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                type_type_0 = DnsRecordPayloadTypeType0(data)



                return type_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(DnsRecordPayloadTypeType0 | None | Unset, data)

        type_ = _parse_type_(d.pop("type", UNSET))


        def _parse_values(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                values_type_0 = cast(list[str], data)

                return values_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        values = _parse_values(d.pop("values", UNSET))


        dns_record_payload = cls(
            address=address,
            ttl=ttl,
            type_=type_,
            values=values,
        )

        return dns_record_payload

