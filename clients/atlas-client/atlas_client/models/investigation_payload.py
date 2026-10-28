from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.investigation_payload_context_type_0 import InvestigationPayloadContextType0





T = TypeVar("T", bound="InvestigationPayload")



@_attrs_define
class InvestigationPayload:
    """ One problem for the read-only investigator of the region.

        Attributes:
            problem (str): The problem to investigate.
            context (InvestigationPayloadContextType0 | None | Unset): Facts that help, such as resource IDs, alert names,
                and times.
            requested_by (None | str | Unset): Email of the person who asked.
            title (None | str | Unset): Short title. The default is the start of the problem.
     """

    problem: str
    context: InvestigationPayloadContextType0 | None | Unset = UNSET
    requested_by: None | str | Unset = UNSET
    title: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.investigation_payload_context_type_0 import InvestigationPayloadContextType0 # noqa: PLC0415
        problem = self.problem

        context: dict[str, Any] | None | Unset
        if isinstance(self.context, Unset):
            context = UNSET
        elif isinstance(self.context, InvestigationPayloadContextType0):
            context = self.context.to_dict()
        else:
            context = self.context

        requested_by: None | str | Unset
        if isinstance(self.requested_by, Unset):
            requested_by = UNSET
        else:
            requested_by = self.requested_by

        title: None | str | Unset
        if isinstance(self.title, Unset):
            title = UNSET
        else:
            title = self.title


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "problem": problem,
        })
        if context is not UNSET:
            field_dict["context"] = context
        if requested_by is not UNSET:
            field_dict["requested_by"] = requested_by
        if title is not UNSET:
            field_dict["title"] = title

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.investigation_payload_context_type_0 import InvestigationPayloadContextType0 # noqa: PLC0415
        d = dict(src_dict)
        problem = d.pop("problem")

        def _parse_context(data: object) -> InvestigationPayloadContextType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                context_type_0 = InvestigationPayloadContextType0.from_dict(data)



                return context_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(InvestigationPayloadContextType0 | None | Unset, data)

        context = _parse_context(d.pop("context", UNSET))


        def _parse_requested_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        requested_by = _parse_requested_by(d.pop("requested_by", UNSET))


        def _parse_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        title = _parse_title(d.pop("title", UNSET))


        investigation_payload = cls(
            problem=problem,
            context=context,
            requested_by=requested_by,
            title=title,
        )

        return investigation_payload

