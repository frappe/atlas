""" Contains all the data models used in inputs/outputs """

from .api_error_field import ApiErrorField
from .capacity_unavailable_response import CapacityUnavailableResponse
from .configure_webhooks_payload import ConfigureWebhooksPayload
from .console_token_payload import ConsoleTokenPayload
from .console_token_payload_mode import ConsoleTokenPayloadMode
from .console_token_response import ConsoleTokenResponse
from .console_token_response_mode import ConsoleTokenResponseMode
from .create_virtual_machine_payload import CreateVirtualMachinePayload
from .create_virtual_machine_payload_egress import CreateVirtualMachinePayloadEgress
from .create_virtual_machine_payload_metadata import CreateVirtualMachinePayloadMetadata
from .disk_update_payload import DiskUpdatePayload
from .download_image_artifact import DownloadImageArtifact
from .firewall_payload import FirewallPayload
from .firewall_response import FirewallResponse
from .firewall_rule_payload import FirewallRulePayload
from .firewall_rule_payload_protocol import FirewallRulePayloadProtocol
from .firewall_rule_response import FirewallRuleResponse
from .firewall_rule_response_protocol import FirewallRuleResponseProtocol
from .firewall_update_payload import FirewallUpdatePayload
from .image_download_response import ImageDownloadResponse
from .image_download_response_artifact import ImageDownloadResponseArtifact
from .image_response import ImageResponse
from .image_response_tags import ImageResponseTags
from .ip_address_assignment_payload import IPAddressAssignmentPayload
from .ip_address_response import IPAddressResponse
from .ip_address_response_tags import IPAddressResponseTags
from .json_web_key import JSONWebKey
from .json_web_key_set_response import JSONWebKeySetResponse
from .list_images_image_type_type_0 import ListImagesImageTypeType0
from .memory_snapshot_configuration_payload import MemorySnapshotConfigurationPayload
from .metadata_replacement_payload import MetadataReplacementPayload
from .metadata_replacement_payload_metadata import MetadataReplacementPayloadMetadata
from .network_update_payload import NetworkUpdatePayload
from .network_update_payload_egress_type_0 import NetworkUpdatePayloadEgressType0
from .out_of_capacity_error import OutOfCapacityError
from .page_image_response import PageImageResponse
from .page_ip_address_response import PageIPAddressResponse
from .page_virtual_machine_list_response import PageVirtualMachineListResponse
from .placement_busy_error import PlacementBusyError
from .reserve_ip_address_payload import ReserveIPAddressPayload
from .resize_payload import ResizePayload
from .snapshot_payload import SnapshotPayload
from .snapshot_payload_image_type import SnapshotPayloadImageType
from .snapshot_payload_tags import SnapshotPayloadTags
from .ssh_keys_replacement_payload import SSHKeysReplacementPayload
from .termination_protection_payload import TerminationProtectionPayload
from .virtual_machine_compute import VirtualMachineCompute
from .virtual_machine_detail_response import VirtualMachineDetailResponse
from .virtual_machine_detail_response_tags import VirtualMachineDetailResponseTags
from .virtual_machine_disk import VirtualMachineDisk
from .virtual_machine_guest import VirtualMachineGuest
from .virtual_machine_guest_metadata import VirtualMachineGuestMetadata
from .virtual_machine_list_response import VirtualMachineListResponse
from .virtual_machine_list_response_tags import VirtualMachineListResponseTags
from .virtual_machine_network import VirtualMachineNetwork
from .virtual_machine_response import VirtualMachineResponse
from .virtual_machine_response_tags import VirtualMachineResponseTags
from .webhook_configuration_response import WebhookConfigurationResponse

__all__ = (
    "ApiErrorField",
    "CapacityUnavailableResponse",
    "ConfigureWebhooksPayload",
    "ConsoleTokenPayload",
    "ConsoleTokenPayloadMode",
    "ConsoleTokenResponse",
    "ConsoleTokenResponseMode",
    "CreateVirtualMachinePayload",
    "CreateVirtualMachinePayloadEgress",
    "CreateVirtualMachinePayloadMetadata",
    "DiskUpdatePayload",
    "DownloadImageArtifact",
    "FirewallPayload",
    "FirewallResponse",
    "FirewallRulePayload",
    "FirewallRulePayloadProtocol",
    "FirewallRuleResponse",
    "FirewallRuleResponseProtocol",
    "FirewallUpdatePayload",
    "ImageDownloadResponse",
    "ImageDownloadResponseArtifact",
    "ImageResponse",
    "ImageResponseTags",
    "IPAddressAssignmentPayload",
    "IPAddressResponse",
    "IPAddressResponseTags",
    "JSONWebKey",
    "JSONWebKeySetResponse",
    "ListImagesImageTypeType0",
    "MemorySnapshotConfigurationPayload",
    "MetadataReplacementPayload",
    "MetadataReplacementPayloadMetadata",
    "NetworkUpdatePayload",
    "NetworkUpdatePayloadEgressType0",
    "OutOfCapacityError",
    "PageImageResponse",
    "PageIPAddressResponse",
    "PageVirtualMachineListResponse",
    "PlacementBusyError",
    "ReserveIPAddressPayload",
    "ResizePayload",
    "SnapshotPayload",
    "SnapshotPayloadImageType",
    "SnapshotPayloadTags",
    "SSHKeysReplacementPayload",
    "TerminationProtectionPayload",
    "VirtualMachineCompute",
    "VirtualMachineDetailResponse",
    "VirtualMachineDetailResponseTags",
    "VirtualMachineDisk",
    "VirtualMachineGuest",
    "VirtualMachineGuestMetadata",
    "VirtualMachineListResponse",
    "VirtualMachineListResponseTags",
    "VirtualMachineNetwork",
    "VirtualMachineResponse",
    "VirtualMachineResponseTags",
    "WebhookConfigurationResponse",
)
