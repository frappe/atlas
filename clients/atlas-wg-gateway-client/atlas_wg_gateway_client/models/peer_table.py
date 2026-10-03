from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.restored_peer import RestoredPeer





T = TypeVar("T", bound="PeerTable")



@_attrs_define
class PeerTable:
    """ The complete peer table, for a restore.

        Attributes:
            peers (list[RestoredPeer]): Every peer. A peer without node_id is assigned like a new one.
     """

    peers: list[RestoredPeer]





    def to_dict(self) -> dict[str, Any]:
        from ..models.restored_peer import RestoredPeer # noqa: PLC0415
        peers = []
        for peers_item_data in self.peers:
            peers_item = peers_item_data.to_dict()
            peers.append(peers_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "peers": peers,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.restored_peer import RestoredPeer # noqa: PLC0415
        d = dict(src_dict)
        peers = []
        _peers = d.pop("peers")
        for peers_item_data in (_peers):
            peers_item = RestoredPeer.from_dict(peers_item_data)



            peers.append(peers_item)


        peer_table = cls(
            peers=peers,
        )

        return peer_table

