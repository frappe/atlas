from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PeerRegistration")



@_attrs_define
class PeerRegistration:
    """ One peer to register.

        Attributes:
            client_id (int): Peer number, unique within the tenant.
            public_key (str): WireGuard public key of the peer.
            tenant_id (int): Tenant that owns the VMs the peer reaches.
     """

    client_id: int
    public_key: str
    tenant_id: int





    def to_dict(self) -> dict[str, Any]:
        client_id = self.client_id

        public_key = self.public_key

        tenant_id = self.tenant_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "client_id": client_id,
            "public_key": public_key,
            "tenant_id": tenant_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        client_id = d.pop("client_id")

        public_key = d.pop("public_key")

        tenant_id = d.pop("tenant_id")

        peer_registration = cls(
            client_id=client_id,
            public_key=public_key,
            tenant_id=tenant_id,
        )

        return peer_registration

