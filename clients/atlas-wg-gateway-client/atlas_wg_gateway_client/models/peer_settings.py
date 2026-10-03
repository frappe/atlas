from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="PeerSettings")



@_attrs_define
class PeerSettings:
    """ The WireGuard settings of one peer.

        Attributes:
            address (str): Interface address of the peer.
            allowed_ips (list[str]): The VM addresses of the tenant. The peer puts them in its own AllowedIPs.
            client_id (int): Peer number within the tenant.
            endpoint (str): Node that serves the peer. Empty when that node was archived.
            public_key (str): Public key of that node. Empty when that node was archived.
            tenant_id (int): Tenant of the peer.
     """

    address: str
    allowed_ips: list[str]
    client_id: int
    endpoint: str
    public_key: str
    tenant_id: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        address = self.address

        allowed_ips = self.allowed_ips



        client_id = self.client_id

        endpoint = self.endpoint

        public_key = self.public_key

        tenant_id = self.tenant_id


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "address": address,
            "allowed_ips": allowed_ips,
            "client_id": client_id,
            "endpoint": endpoint,
            "public_key": public_key,
            "tenant_id": tenant_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        address = d.pop("address")

        allowed_ips = cast(list[str], d.pop("allowed_ips"))


        client_id = d.pop("client_id")

        endpoint = d.pop("endpoint")

        public_key = d.pop("public_key")

        tenant_id = d.pop("tenant_id")

        peer_settings = cls(
            address=address,
            allowed_ips=allowed_ips,
            client_id=client_id,
            endpoint=endpoint,
            public_key=public_key,
            tenant_id=tenant_id,
        )


        peer_settings.additional_properties = d
        return peer_settings

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
