from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.affinity_unsatisfied_error import AffinityUnsatisfiedError
  from ..models.out_of_capacity_error import OutOfCapacityError
  from ..models.placement_busy_error import PlacementBusyError





T = TypeVar("T", bound="CapacityUnavailableResponse")



@_attrs_define
class CapacityUnavailableResponse:
    """ The JSON body of a 503 from virtual machine creation.

    `error.code` separates a fleet that is full from one that is only busy, so a
    caller can retry a busy placement at once and escalate a full one.

        Attributes:
            error (AffinityUnsatisfiedError | OutOfCapacityError | PlacementBusyError): Capacity failure details.
     """

    error: AffinityUnsatisfiedError | OutOfCapacityError | PlacementBusyError
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.affinity_unsatisfied_error import AffinityUnsatisfiedError # noqa: PLC0415
        from ..models.out_of_capacity_error import OutOfCapacityError # noqa: PLC0415
        from ..models.placement_busy_error import PlacementBusyError # noqa: PLC0415
        error: dict[str, Any]
        if isinstance(self.error, OutOfCapacityError):
            error = self.error.to_dict()
        elif isinstance(self.error, PlacementBusyError):
            error = self.error.to_dict()
        else:
            error = self.error.to_dict()



        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "error": error,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.affinity_unsatisfied_error import AffinityUnsatisfiedError # noqa: PLC0415
        from ..models.out_of_capacity_error import OutOfCapacityError # noqa: PLC0415
        from ..models.placement_busy_error import PlacementBusyError # noqa: PLC0415
        d = dict(src_dict)
        def _parse_error(data: object) -> AffinityUnsatisfiedError | OutOfCapacityError | PlacementBusyError:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                error_type_0 = OutOfCapacityError.from_dict(data)



                return error_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                error_type_1 = PlacementBusyError.from_dict(data)



                return error_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            error_type_2 = AffinityUnsatisfiedError.from_dict(data)



            return error_type_2

        error = _parse_error(d.pop("error"))


        capacity_unavailable_response = cls(
            error=error,
        )


        capacity_unavailable_response.additional_properties = d
        return capacity_unavailable_response

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
