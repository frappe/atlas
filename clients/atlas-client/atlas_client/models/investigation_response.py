from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.investigation_response_status import InvestigationResponseStatus
from typing import cast

if TYPE_CHECKING:
  from ..models.investigation_response_context_type_0 import InvestigationResponseContextType0





T = TypeVar("T", bound="InvestigationResponse")



@_attrs_define
class InvestigationResponse:
    """ One investigation. Atlas masks credentials in each text field.

        Attributes:
            context (InvestigationResponseContextType0 | None): Facts sent with the problem.
            created_at (int): Creation time as Unix seconds.
            error (None | str): Why the investigation failed.
            id (str): Investigation ID.
            outcome (None | str): The report in Markdown, when the investigation is complete.
            problem (str): The problem to investigate.
            status (InvestigationResponseStatus): Investigation state.
            title (str): Short title.
     """

    context: InvestigationResponseContextType0 | None
    created_at: int
    error: None | str
    id: str
    outcome: None | str
    problem: str
    status: InvestigationResponseStatus
    title: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.investigation_response_context_type_0 import InvestigationResponseContextType0 # noqa: PLC0415
        context: dict[str, Any] | None
        if isinstance(self.context, InvestigationResponseContextType0):
            context = self.context.to_dict()
        else:
            context = self.context

        created_at = self.created_at

        error: None | str
        error = self.error

        id = self.id

        outcome: None | str
        outcome = self.outcome

        problem = self.problem

        status = self.status.value

        title = self.title


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "context": context,
            "created_at": created_at,
            "error": error,
            "id": id,
            "outcome": outcome,
            "problem": problem,
            "status": status,
            "title": title,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.investigation_response_context_type_0 import InvestigationResponseContextType0 # noqa: PLC0415
        d = dict(src_dict)
        def _parse_context(data: object) -> InvestigationResponseContextType0 | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                context_type_0 = InvestigationResponseContextType0.from_dict(data)



                return context_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(InvestigationResponseContextType0 | None, data)

        context = _parse_context(d.pop("context"))


        created_at = d.pop("created_at")

        def _parse_error(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        error = _parse_error(d.pop("error"))


        id = d.pop("id")

        def _parse_outcome(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        outcome = _parse_outcome(d.pop("outcome"))


        problem = d.pop("problem")

        status = InvestigationResponseStatus(d.pop("status"))




        title = d.pop("title")

        investigation_response = cls(
            context=context,
            created_at=created_at,
            error=error,
            id=id,
            outcome=outcome,
            problem=problem,
            status=status,
            title=title,
        )


        investigation_response.additional_properties = d
        return investigation_response

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
