from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="ListedPeer")



@_attrs_define
class ListedPeer:
    """ One registered peer and the node that serves it.

        Attributes:
            tenant_id (int): Tenant of the peer.
            client_id (int): Peer number within the tenant.
            address (str): Interface address of the peer.
            allowed_ips (list[str]): The VM addresses of the tenant. The peer puts them in its own AllowedIPs.
            endpoint (str): Node that serves the peer. Empty when that node was archived.
            public_key (str): Public key of that node. Empty when that node was archived.
            node_id (str): Node that serves the peer.
     """

    tenant_id: int
    client_id: int
    address: str
    allowed_ips: list[str]
    endpoint: str
    public_key: str
    node_id: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        tenant_id = self.tenant_id

        client_id = self.client_id

        address = self.address

        allowed_ips = self.allowed_ips



        endpoint = self.endpoint

        public_key = self.public_key

        node_id = self.node_id


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "tenant_id": tenant_id,
            "client_id": client_id,
            "address": address,
            "allowed_ips": allowed_ips,
            "endpoint": endpoint,
            "public_key": public_key,
            "node_id": node_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        tenant_id = d.pop("tenant_id")

        client_id = d.pop("client_id")

        address = d.pop("address")

        allowed_ips = cast(list[str], d.pop("allowed_ips"))


        endpoint = d.pop("endpoint")

        public_key = d.pop("public_key")

        node_id = d.pop("node_id")

        listed_peer = cls(
            tenant_id=tenant_id,
            client_id=client_id,
            address=address,
            allowed_ips=allowed_ips,
            endpoint=endpoint,
            public_key=public_key,
            node_id=node_id,
        )


        listed_peer.additional_properties = d
        return listed_peer

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
