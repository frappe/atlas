from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.affinity_rule_payload_operator import AffinityRulePayloadOperator
from ..models.affinity_rule_payload_resource import AffinityRulePayloadResource
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.affinity_rule_payload_tags import AffinityRulePayloadTags





T = TypeVar("T", bound="AffinityRulePayload")



@_attrs_define
class AffinityRulePayload:
    """ One rule on the tags of the candidate Metal Server, or of a VM that runs on it.

        Attributes:
            operator (AffinityRulePayloadOperator): `has` needs a resource with every tag pair. `has_not` rejects such a
                resource.
            resource (AffinityRulePayloadResource): `metal_server` checks the candidate host. `virtual_machine` checks the
                VMs on the candidate host.
            tags (AffinityRulePayloadTags): Tag pairs that must all be on one resource.
            within (None | str | Unset): A host tag key, such as `rack`. A `virtual_machine` rule then reads the VMs on
                every host that has the same value for this key as the candidate host. A host without the key fails the rule.
     """

    operator: AffinityRulePayloadOperator
    resource: AffinityRulePayloadResource
    tags: AffinityRulePayloadTags
    within: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.affinity_rule_payload_tags import AffinityRulePayloadTags # noqa: PLC0415
        operator = self.operator.value

        resource = self.resource.value

        tags = self.tags.to_dict()

        within: None | str | Unset
        if isinstance(self.within, Unset):
            within = UNSET
        else:
            within = self.within


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "operator": operator,
            "resource": resource,
            "tags": tags,
        })
        if within is not UNSET:
            field_dict["within"] = within

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.affinity_rule_payload_tags import AffinityRulePayloadTags # noqa: PLC0415
        d = dict(src_dict)
        operator = AffinityRulePayloadOperator(d.pop("operator"))




        resource = AffinityRulePayloadResource(d.pop("resource"))




        tags = AffinityRulePayloadTags.from_dict(d.pop("tags"))




        def _parse_within(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        within = _parse_within(d.pop("within", UNSET))


        affinity_rule_payload = cls(
            operator=operator,
            resource=resource,
            tags=tags,
            within=within,
        )

        return affinity_rule_payload

