from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.dns_record_payload_type import DnsRecordPayloadType
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.health_check import HealthCheck





T = TypeVar("T", bound="DnsRecordPayload")



@_attrs_define
class DnsRecordPayload:
    """ The records of one regional service name, or of one name under it.

        Attributes:
            type_ (DnsRecordPayloadType): Record type.
            values (list[str]): Addresses for A and AAAA, unquoted values for TXT, or one host name for CNAME. They replace
                the values the name had.
            health_check (HealthCheck | None | Unset): Give each address of a public A or AAAA name its own health check.
                DNS leaves out an unhealthy address while another address is healthy.
            ttl (int | Unset): Record TTL in seconds. Default: 300.
     """

    type_: DnsRecordPayloadType
    values: list[str]
    health_check: HealthCheck | None | Unset = UNSET
    ttl: int | Unset = 300





    def to_dict(self) -> dict[str, Any]:
        from ..models.health_check import HealthCheck # noqa: PLC0415
        type_ = self.type_.value

        values = self.values



        health_check: dict[str, Any] | None | Unset
        if isinstance(self.health_check, Unset):
            health_check = UNSET
        elif isinstance(self.health_check, HealthCheck):
            health_check = self.health_check.to_dict()
        else:
            health_check = self.health_check

        ttl = self.ttl


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "type": type_,
            "values": values,
        })
        if health_check is not UNSET:
            field_dict["health_check"] = health_check
        if ttl is not UNSET:
            field_dict["ttl"] = ttl

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.health_check import HealthCheck # noqa: PLC0415
        d = dict(src_dict)
        type_ = DnsRecordPayloadType(d.pop("type"))




        values = cast(list[str], d.pop("values"))


        def _parse_health_check(data: object) -> HealthCheck | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                health_check_type_0 = HealthCheck.from_dict(data)



                return health_check_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(HealthCheck | None | Unset, data)

        health_check = _parse_health_check(d.pop("health_check", UNSET))


        ttl = d.pop("ttl", UNSET)

        dns_record_payload = cls(
            type_=type_,
            values=values,
            health_check=health_check,
            ttl=ttl,
        )

        return dns_record_payload

