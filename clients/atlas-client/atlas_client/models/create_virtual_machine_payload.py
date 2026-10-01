from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.affinity_all_of_payload import AffinityAllOfPayload
  from ..models.affinity_any_of_payload import AffinityAnyOfPayload
  from ..models.affinity_rule_payload import AffinityRulePayload
  from ..models.create_virtual_machine_payload_metadata import CreateVirtualMachinePayloadMetadata
  from ..models.create_virtual_machine_payload_tags import CreateVirtualMachinePayloadTags
  from ..models.firewall_payload import FirewallPayload





T = TypeVar("T", bound="CreateVirtualMachinePayload")



@_attrs_define
class CreateVirtualMachinePayload:
    """ Values that create one virtual machine.

        Attributes:
            cpu_millicores (int): CPU capacity in millicores. 1000 millicores equals one virtual CPU.
            disk_mib (int): Root disk capacity in MiB.
            image_id (str): Image used to create the virtual machine.
            memory_mib (int): Memory capacity in MiB.
            disk_iops (int | Unset): Disk IOPS limit. Zero removes the limit. Default: 0.
            disk_throughput_mibps (int | Unset): Disk throughput limit in MiB/s. Zero removes the limit. Default: 0.
            firewall (FirewallPayload | Unset): The complete desired firewall configuration.
            hostname (str | Unset): Guest hostname. Default: ''.
            ipv4_internet_access (bool | Unset): Reach the IPv4 internet through host NAT. A public IPv4 address needs it.
                Without it and without a public IPv6 address, the VM reaches only the mesh. Default: True.
            is_privileged (bool | Unset): Whether the guest can reach every tenant through the mesh. Default: False.
            is_termination_protected (bool | Unset): Whether deletion is blocked. Default: False.
            metadata (CreateVirtualMachinePayloadMetadata | Unset): Custom guest metadata.
            placement_rules (list[AffinityAllOfPayload | AffinityAnyOfPayload | AffinityRulePayload] | Unset): Rules that
                limit the Metal Servers for the virtual machine. Every listed rule or group must hold. Only a System Manager can
                set them, and only on a privileged virtual machine. Placement uses only the hosts that meet them.
            private_network_throughput_mibps (int | Unset): Private network throughput limit in MiB/s. Zero removes the
                limit. Default: 0.
            public_ipv4 (None | str | Unset): Reserved public IPv4 allocation ID, or null.
            public_ipv6 (None | str | Unset): Reserved public IPv6 allocation ID, or null.
            public_network_throughput_mibps (int | Unset): Public network throughput limit in MiB/s. Zero removes the limit.
                Default: 0.
            sleep_after_idle_seconds (int | Unset): Idle time before automatic stop. Zero disables it. Default: 0.
            ssh_keys (list[str] | Unset): Authorized SSH public keys.
            tags (CreateVirtualMachinePayloadTags | Unset): Resource tags as key-value pairs.
            user_data (str | Unset): Cloud-init user data supplied to the guest. Default: ''.
     """

    cpu_millicores: int
    disk_mib: int
    image_id: str
    memory_mib: int
    disk_iops: int | Unset = 0
    disk_throughput_mibps: int | Unset = 0
    firewall: FirewallPayload | Unset = UNSET
    hostname: str | Unset = ''
    ipv4_internet_access: bool | Unset = True
    is_privileged: bool | Unset = False
    is_termination_protected: bool | Unset = False
    metadata: CreateVirtualMachinePayloadMetadata | Unset = UNSET
    placement_rules: list[AffinityAllOfPayload | AffinityAnyOfPayload | AffinityRulePayload] | Unset = UNSET
    private_network_throughput_mibps: int | Unset = 0
    public_ipv4: None | str | Unset = UNSET
    public_ipv6: None | str | Unset = UNSET
    public_network_throughput_mibps: int | Unset = 0
    sleep_after_idle_seconds: int | Unset = 0
    ssh_keys: list[str] | Unset = UNSET
    tags: CreateVirtualMachinePayloadTags | Unset = UNSET
    user_data: str | Unset = ''





    def to_dict(self) -> dict[str, Any]:
        from ..models.affinity_all_of_payload import AffinityAllOfPayload # noqa: PLC0415
        from ..models.affinity_any_of_payload import AffinityAnyOfPayload # noqa: PLC0415
        from ..models.affinity_rule_payload import AffinityRulePayload # noqa: PLC0415
        from ..models.create_virtual_machine_payload_metadata import CreateVirtualMachinePayloadMetadata # noqa: PLC0415
        from ..models.create_virtual_machine_payload_tags import CreateVirtualMachinePayloadTags # noqa: PLC0415
        from ..models.firewall_payload import FirewallPayload # noqa: PLC0415
        cpu_millicores = self.cpu_millicores

        disk_mib = self.disk_mib

        image_id = self.image_id

        memory_mib = self.memory_mib

        disk_iops = self.disk_iops

        disk_throughput_mibps = self.disk_throughput_mibps

        firewall: dict[str, Any] | Unset = UNSET
        if not isinstance(self.firewall, Unset):
            firewall = self.firewall.to_dict()

        hostname = self.hostname

        ipv4_internet_access = self.ipv4_internet_access

        is_privileged = self.is_privileged

        is_termination_protected = self.is_termination_protected

        metadata: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metadata, Unset):
            metadata = self.metadata.to_dict()

        placement_rules: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.placement_rules, Unset):
            placement_rules = []
            for placement_rules_item_data in self.placement_rules:
                placement_rules_item: dict[str, Any]
                if isinstance(placement_rules_item_data, AffinityRulePayload):
                    placement_rules_item = placement_rules_item_data.to_dict()
                elif isinstance(placement_rules_item_data, AffinityAnyOfPayload):
                    placement_rules_item = placement_rules_item_data.to_dict()
                else:
                    placement_rules_item = placement_rules_item_data.to_dict()

                placement_rules.append(placement_rules_item)



        private_network_throughput_mibps = self.private_network_throughput_mibps

        public_ipv4: None | str | Unset
        if isinstance(self.public_ipv4, Unset):
            public_ipv4 = UNSET
        else:
            public_ipv4 = self.public_ipv4

        public_ipv6: None | str | Unset
        if isinstance(self.public_ipv6, Unset):
            public_ipv6 = UNSET
        else:
            public_ipv6 = self.public_ipv6

        public_network_throughput_mibps = self.public_network_throughput_mibps

        sleep_after_idle_seconds = self.sleep_after_idle_seconds

        ssh_keys: list[str] | Unset = UNSET
        if not isinstance(self.ssh_keys, Unset):
            ssh_keys = self.ssh_keys



        tags: dict[str, Any] | Unset = UNSET
        if not isinstance(self.tags, Unset):
            tags = self.tags.to_dict()

        user_data = self.user_data


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "cpu_millicores": cpu_millicores,
            "disk_mib": disk_mib,
            "image_id": image_id,
            "memory_mib": memory_mib,
        })
        if disk_iops is not UNSET:
            field_dict["disk_iops"] = disk_iops
        if disk_throughput_mibps is not UNSET:
            field_dict["disk_throughput_mibps"] = disk_throughput_mibps
        if firewall is not UNSET:
            field_dict["firewall"] = firewall
        if hostname is not UNSET:
            field_dict["hostname"] = hostname
        if ipv4_internet_access is not UNSET:
            field_dict["ipv4_internet_access"] = ipv4_internet_access
        if is_privileged is not UNSET:
            field_dict["is_privileged"] = is_privileged
        if is_termination_protected is not UNSET:
            field_dict["is_termination_protected"] = is_termination_protected
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if placement_rules is not UNSET:
            field_dict["placement_rules"] = placement_rules
        if private_network_throughput_mibps is not UNSET:
            field_dict["private_network_throughput_mibps"] = private_network_throughput_mibps
        if public_ipv4 is not UNSET:
            field_dict["public_ipv4"] = public_ipv4
        if public_ipv6 is not UNSET:
            field_dict["public_ipv6"] = public_ipv6
        if public_network_throughput_mibps is not UNSET:
            field_dict["public_network_throughput_mibps"] = public_network_throughput_mibps
        if sleep_after_idle_seconds is not UNSET:
            field_dict["sleep_after_idle_seconds"] = sleep_after_idle_seconds
        if ssh_keys is not UNSET:
            field_dict["ssh_keys"] = ssh_keys
        if tags is not UNSET:
            field_dict["tags"] = tags
        if user_data is not UNSET:
            field_dict["user_data"] = user_data

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.affinity_all_of_payload import AffinityAllOfPayload # noqa: PLC0415
        from ..models.affinity_any_of_payload import AffinityAnyOfPayload # noqa: PLC0415
        from ..models.affinity_rule_payload import AffinityRulePayload # noqa: PLC0415
        from ..models.create_virtual_machine_payload_metadata import CreateVirtualMachinePayloadMetadata # noqa: PLC0415
        from ..models.create_virtual_machine_payload_tags import CreateVirtualMachinePayloadTags # noqa: PLC0415
        from ..models.firewall_payload import FirewallPayload # noqa: PLC0415
        d = dict(src_dict)
        cpu_millicores = d.pop("cpu_millicores")

        disk_mib = d.pop("disk_mib")

        image_id = d.pop("image_id")

        memory_mib = d.pop("memory_mib")

        disk_iops = d.pop("disk_iops", UNSET)

        disk_throughput_mibps = d.pop("disk_throughput_mibps", UNSET)

        _firewall = d.pop("firewall", UNSET)
        firewall: FirewallPayload | Unset
        if isinstance(_firewall,  Unset):
            firewall = UNSET
        else:
            firewall = FirewallPayload.from_dict(_firewall)




        hostname = d.pop("hostname", UNSET)

        ipv4_internet_access = d.pop("ipv4_internet_access", UNSET)

        is_privileged = d.pop("is_privileged", UNSET)

        is_termination_protected = d.pop("is_termination_protected", UNSET)

        _metadata = d.pop("metadata", UNSET)
        metadata: CreateVirtualMachinePayloadMetadata | Unset
        if isinstance(_metadata,  Unset):
            metadata = UNSET
        else:
            metadata = CreateVirtualMachinePayloadMetadata.from_dict(_metadata)




        _placement_rules = d.pop("placement_rules", UNSET)
        placement_rules: list[AffinityAllOfPayload | AffinityAnyOfPayload | AffinityRulePayload] | Unset = UNSET
        if _placement_rules is not UNSET:
            placement_rules = []
            for placement_rules_item_data in _placement_rules:
                def _parse_placement_rules_item(data: object) -> AffinityAllOfPayload | AffinityAnyOfPayload | AffinityRulePayload:
                    try:
                        if not isinstance(data, dict):
                            raise TypeError()
                        placement_rules_item_type_0 = AffinityRulePayload.from_dict(data)



                        return placement_rules_item_type_0
                    except (TypeError, ValueError, AttributeError, KeyError):
                        pass
                    try:
                        if not isinstance(data, dict):
                            raise TypeError()
                        placement_rules_item_type_1 = AffinityAnyOfPayload.from_dict(data)



                        return placement_rules_item_type_1
                    except (TypeError, ValueError, AttributeError, KeyError):
                        pass
                    if not isinstance(data, dict):
                        raise TypeError()
                    placement_rules_item_type_2 = AffinityAllOfPayload.from_dict(data)



                    return placement_rules_item_type_2

                placement_rules_item = _parse_placement_rules_item(placement_rules_item_data)

                placement_rules.append(placement_rules_item)


        private_network_throughput_mibps = d.pop("private_network_throughput_mibps", UNSET)

        def _parse_public_ipv4(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        public_ipv4 = _parse_public_ipv4(d.pop("public_ipv4", UNSET))


        def _parse_public_ipv6(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        public_ipv6 = _parse_public_ipv6(d.pop("public_ipv6", UNSET))


        public_network_throughput_mibps = d.pop("public_network_throughput_mibps", UNSET)

        sleep_after_idle_seconds = d.pop("sleep_after_idle_seconds", UNSET)

        ssh_keys = cast(list[str], d.pop("ssh_keys", UNSET))


        _tags = d.pop("tags", UNSET)
        tags: CreateVirtualMachinePayloadTags | Unset
        if isinstance(_tags,  Unset):
            tags = UNSET
        else:
            tags = CreateVirtualMachinePayloadTags.from_dict(_tags)




        user_data = d.pop("user_data", UNSET)

        create_virtual_machine_payload = cls(
            cpu_millicores=cpu_millicores,
            disk_mib=disk_mib,
            image_id=image_id,
            memory_mib=memory_mib,
            disk_iops=disk_iops,
            disk_throughput_mibps=disk_throughput_mibps,
            firewall=firewall,
            hostname=hostname,
            ipv4_internet_access=ipv4_internet_access,
            is_privileged=is_privileged,
            is_termination_protected=is_termination_protected,
            metadata=metadata,
            placement_rules=placement_rules,
            private_network_throughput_mibps=private_network_throughput_mibps,
            public_ipv4=public_ipv4,
            public_ipv6=public_ipv6,
            public_network_throughput_mibps=public_network_throughput_mibps,
            sleep_after_idle_seconds=sleep_after_idle_seconds,
            ssh_keys=ssh_keys,
            tags=tags,
            user_data=user_data,
        )

        return create_virtual_machine_payload

