from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.virtual_machine_metrics_sample import VirtualMachineMetricsSample





T = TypeVar("T", bound="VirtualMachineMetricsResponse")



@_attrs_define
class VirtualMachineMetricsResponse:
    """ 
        Attributes:
            id (str):
            samples (list[VirtualMachineMetricsSample]):
            sample_interval_seconds (int | Unset): Seconds covered by each sample. Default: 0.
     """

    id: str
    samples: list[VirtualMachineMetricsSample]
    sample_interval_seconds: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.virtual_machine_metrics_sample import VirtualMachineMetricsSample # noqa: PLC0415
        id = self.id

        samples = []
        for samples_item_data in self.samples:
            samples_item = samples_item_data.to_dict()
            samples.append(samples_item)



        sample_interval_seconds = self.sample_interval_seconds


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "id": id,
            "samples": samples,
        })
        if sample_interval_seconds is not UNSET:
            field_dict["sample_interval_seconds"] = sample_interval_seconds

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.virtual_machine_metrics_sample import VirtualMachineMetricsSample # noqa: PLC0415
        d = dict(src_dict)
        id = d.pop("id")

        samples = []
        _samples = d.pop("samples")
        for samples_item_data in (_samples):
            samples_item = VirtualMachineMetricsSample.from_dict(samples_item_data)



            samples.append(samples_item)


        sample_interval_seconds = d.pop("sample_interval_seconds", UNSET)

        virtual_machine_metrics_response = cls(
            id=id,
            samples=samples,
            sample_interval_seconds=sample_interval_seconds,
        )


        virtual_machine_metrics_response.additional_properties = d
        return virtual_machine_metrics_response

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
