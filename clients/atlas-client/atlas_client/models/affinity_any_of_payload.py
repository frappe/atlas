from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.affinity_all_of_payload import AffinityAllOfPayload
  from ..models.affinity_rule_payload import AffinityRulePayload





T = TypeVar("T", bound="AffinityAnyOfPayload")



@_attrs_define
class AffinityAnyOfPayload:
    """ A group that holds when at least one of its rules or groups holds.

        Attributes:
            any_of (list[AffinityAllOfPayload | AffinityAnyOfPayload | AffinityRulePayload]): Rules or groups. One must
                hold.
     """

    any_of: list[AffinityAllOfPayload | AffinityAnyOfPayload | AffinityRulePayload]





    def to_dict(self) -> dict[str, Any]:
        from ..models.affinity_all_of_payload import AffinityAllOfPayload # noqa: PLC0415
        from ..models.affinity_rule_payload import AffinityRulePayload # noqa: PLC0415
        any_of = []
        for any_of_item_data in self.any_of:
            any_of_item: dict[str, Any]
            if isinstance(any_of_item_data, AffinityRulePayload):
                any_of_item = any_of_item_data.to_dict()
            elif isinstance(any_of_item_data, AffinityAnyOfPayload):
                any_of_item = any_of_item_data.to_dict()
            else:
                any_of_item = any_of_item_data.to_dict()

            any_of.append(any_of_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "any_of": any_of,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.affinity_all_of_payload import AffinityAllOfPayload # noqa: PLC0415
        from ..models.affinity_rule_payload import AffinityRulePayload # noqa: PLC0415
        d = dict(src_dict)
        any_of = []
        _any_of = d.pop("any_of")
        for any_of_item_data in (_any_of):
            def _parse_any_of_item(data: object) -> AffinityAllOfPayload | AffinityAnyOfPayload | AffinityRulePayload:
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    any_of_item_type_0 = AffinityRulePayload.from_dict(data)



                    return any_of_item_type_0
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    any_of_item_type_1 = AffinityAnyOfPayload.from_dict(data)



                    return any_of_item_type_1
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                if not isinstance(data, dict):
                    raise TypeError()
                any_of_item_type_2 = AffinityAllOfPayload.from_dict(data)



                return any_of_item_type_2

            any_of_item = _parse_any_of_item(any_of_item_data)

            any_of.append(any_of_item)


        affinity_any_of_payload = cls(
            any_of=any_of,
        )

        return affinity_any_of_payload

