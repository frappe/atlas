from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.affinity_rule_payload_operator import AffinityRulePayloadOperator
from ..models.affinity_rule_payload_resource import AffinityRulePayloadResource
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
     """

    operator: AffinityRulePayloadOperator
    resource: AffinityRulePayloadResource
    tags: AffinityRulePayloadTags





    def to_dict(self) -> dict[str, Any]:
        from ..models.affinity_rule_payload_tags import AffinityRulePayloadTags # noqa: PLC0415
        operator = self.operator.value

        resource = self.resource.value

        tags = self.tags.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "operator": operator,
            "resource": resource,
            "tags": tags,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.affinity_rule_payload_tags import AffinityRulePayloadTags # noqa: PLC0415
        d = dict(src_dict)
        operator = AffinityRulePayloadOperator(d.pop("operator"))




        resource = AffinityRulePayloadResource(d.pop("resource"))




        tags = AffinityRulePayloadTags.from_dict(d.pop("tags"))




        affinity_rule_payload = cls(
            operator=operator,
            resource=resource,
            tags=tags,
        )

        return affinity_rule_payload

