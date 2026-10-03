from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="VirtualMachineNetworkUsage")



@_attrs_define
class VirtualMachineNetworkUsage:
    """ Cumulative unicast IP traffic for the lifetime of the traffic attachment.

    Counters survive guest stops while the attachment remains. Recreating the
    attachment or restarting Metal resets the counters.

        Attributes:
            received_bytes (int): Cumulative bytes received by the guest.
            received_packets (int): Cumulative packets received by the guest.
            sent_bytes (int): Cumulative bytes sent by the guest.
            sent_packets (int): Cumulative packets sent by the guest.
     """

    received_bytes: int
    received_packets: int
    sent_bytes: int
    sent_packets: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        received_bytes = self.received_bytes

        received_packets = self.received_packets

        sent_bytes = self.sent_bytes

        sent_packets = self.sent_packets


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "received_bytes": received_bytes,
            "received_packets": received_packets,
            "sent_bytes": sent_bytes,
            "sent_packets": sent_packets,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        received_bytes = d.pop("received_bytes")

        received_packets = d.pop("received_packets")

        sent_bytes = d.pop("sent_bytes")

        sent_packets = d.pop("sent_packets")

        virtual_machine_network_usage = cls(
            received_bytes=received_bytes,
            received_packets=received_packets,
            sent_bytes=sent_bytes,
            sent_packets=sent_packets,
        )


        virtual_machine_network_usage.additional_properties = d
        return virtual_machine_network_usage

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
