from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.affinity_any_of_payload import AffinityAnyOfPayload
  from ..models.affinity_rule_payload import AffinityRulePayload





T = TypeVar("T", bound="AffinityAllOfPayload")



@_attrs_define
class AffinityAllOfPayload:
    """ A group that holds when every one of its rules or groups holds.

        Attributes:
            all_of (list[AffinityAllOfPayload | AffinityAnyOfPayload | AffinityRulePayload]): Rules or groups. Every one
                must hold.
     """

    all_of: list[AffinityAllOfPayload | AffinityAnyOfPayload | AffinityRulePayload]





    def to_dict(self) -> dict[str, Any]:
        from ..models.affinity_any_of_payload import AffinityAnyOfPayload # noqa: PLC0415
        from ..models.affinity_rule_payload import AffinityRulePayload # noqa: PLC0415
        all_of = []
        for all_of_item_data in self.all_of:
            all_of_item: dict[str, Any]
            if isinstance(all_of_item_data, AffinityRulePayload):
                all_of_item = all_of_item_data.to_dict()
            elif isinstance(all_of_item_data, AffinityAnyOfPayload):
                all_of_item = all_of_item_data.to_dict()
            else:
                all_of_item = all_of_item_data.to_dict()

            all_of.append(all_of_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "all_of": all_of,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.affinity_any_of_payload import AffinityAnyOfPayload # noqa: PLC0415
        from ..models.affinity_rule_payload import AffinityRulePayload # noqa: PLC0415
        d = dict(src_dict)
        all_of = []
        _all_of = d.pop("all_of")
        for all_of_item_data in (_all_of):
            def _parse_all_of_item(data: object) -> AffinityAllOfPayload | AffinityAnyOfPayload | AffinityRulePayload:
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    all_of_item_type_0 = AffinityRulePayload.from_dict(data)



                    return all_of_item_type_0
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    all_of_item_type_1 = AffinityAnyOfPayload.from_dict(data)



                    return all_of_item_type_1
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                if not isinstance(data, dict):
                    raise TypeError()
                all_of_item_type_2 = AffinityAllOfPayload.from_dict(data)



                return all_of_item_type_2

            all_of_item = _parse_all_of_item(all_of_item_data)

            all_of.append(all_of_item)


        affinity_all_of_payload = cls(
            all_of=all_of,
        )

        return affinity_all_of_payload

