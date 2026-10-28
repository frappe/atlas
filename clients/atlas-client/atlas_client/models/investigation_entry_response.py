from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.investigation_entry_response_kind import InvestigationEntryResponseKind
from ..models.investigation_entry_response_status_type_0 import InvestigationEntryResponseStatusType0
from typing import cast






T = TypeVar("T", bound="InvestigationEntryResponse")



@_attrs_define
class InvestigationEntryResponse:
    """ One row of the audit log of an investigation.

        Attributes:
            detail (None | str): Scrubbed arguments and result or error, as JSON.
            is_approved (bool): The person in the chat approved the call.
            kind (InvestigationEntryResponseKind): Tool Call for a read, Action for a change, Decision or Note from the
                agent.
            occurred_at (int): Time as Unix seconds.
            reference_doctype (None | str): Atlas record of the call, such as Metal Server Console Log or SSH Task.
            reference_name (None | str): Name of that record.
            status (InvestigationEntryResponseStatusType0 | None): Result of a tool call.
            title (str): Tool name with its arguments, or the decision.
            user (None | str): Owner of the chat that ran the tool.
     """

    detail: None | str
    is_approved: bool
    kind: InvestigationEntryResponseKind
    occurred_at: int
    reference_doctype: None | str
    reference_name: None | str
    status: InvestigationEntryResponseStatusType0 | None
    title: str
    user: None | str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        detail: None | str
        detail = self.detail

        is_approved = self.is_approved

        kind = self.kind.value

        occurred_at = self.occurred_at

        reference_doctype: None | str
        reference_doctype = self.reference_doctype

        reference_name: None | str
        reference_name = self.reference_name

        status: None | str
        if isinstance(self.status, InvestigationEntryResponseStatusType0):
            status = self.status.value
        else:
            status = self.status

        title = self.title

        user: None | str
        user = self.user


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "detail": detail,
            "is_approved": is_approved,
            "kind": kind,
            "occurred_at": occurred_at,
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "status": status,
            "title": title,
            "user": user,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_detail(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        detail = _parse_detail(d.pop("detail"))


        is_approved = d.pop("is_approved")

        kind = InvestigationEntryResponseKind(d.pop("kind"))




        occurred_at = d.pop("occurred_at")

        def _parse_reference_doctype(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        reference_doctype = _parse_reference_doctype(d.pop("reference_doctype"))


        def _parse_reference_name(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        reference_name = _parse_reference_name(d.pop("reference_name"))


        def _parse_status(data: object) -> InvestigationEntryResponseStatusType0 | None:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                status_type_0 = InvestigationEntryResponseStatusType0(data)



                return status_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(InvestigationEntryResponseStatusType0 | None, data)

        status = _parse_status(d.pop("status"))


        title = d.pop("title")

        def _parse_user(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        user = _parse_user(d.pop("user"))


        investigation_entry_response = cls(
            detail=detail,
            is_approved=is_approved,
            kind=kind,
            occurred_at=occurred_at,
            reference_doctype=reference_doctype,
            reference_name=reference_name,
            status=status,
            title=title,
            user=user,
        )


        investigation_entry_response.additional_properties = d
        return investigation_entry_response

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
