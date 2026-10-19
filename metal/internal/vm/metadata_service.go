package vm

import (
	"encoding/json"
	"strconv"
	"strings"
)

// MetadataServiceSizeLimitBytes bounds the MMDS document and its Firecracker API request.
const MetadataServiceSizeLimitBytes = 256 * 1024

// metadataServiceReserveBytes keeps room for concurrent SSH console keys.
const metadataServiceReserveBytes = 8 * 1024

// MetadataServiceData builds the MMDS document read by the guest.
func (specification Specification) MetadataServiceData(virtualMachineID, ipAddress, macAddress string) map[string]any {
	publicKeys := make(map[string]any, len(specification.SSHKeys))
	for keyIndex, sshKey := range specification.SSHKeys {
		publicKeys[strconv.Itoa(keyIndex)] = map[string]any{"openssh-key": sshKey}
	}

	metadata := map[string]any{"instance-id": virtualMachineID, "public-keys": publicKeys}
	if specification.Hostname != "" {
		metadata["local-hostname"] = specification.Hostname
	}
	if ipAddress != "" {
		metadata["local-ipv4"] = ipAddress
	}
	if macAddress != "" {
		metadata["mac"] = macAddress
	}
	if specification.Network.PublicIPv4 != "" {
		metadata["public-ipv4"] = specification.Network.PublicIPv4
	}
	if routes := strings.Join(HostReachedDestinations(specification.Network), ","); routes != "" {
		metadata["host-routes"] = routes
	}
	if specification.Network.WireGuardMeshIPv6 != "" {
		metadata["mesh-ipv6"] = specification.Network.WireGuardMeshIPv6
	}
	if len(specification.Metadata) > 0 {
		metadata["attributes"] = specification.Metadata
	}

	latest := map[string]any{"meta-data": metadata}
	if specification.UserData != "" && !specification.Rescue.Enabled {
		latest["user-data"] = specification.UserData
	}
	return map[string]any{"latest": latest}
}

// validateMetadataServiceSize measures the document with the longest guest address values.
func (specification Specification) validateMetadataServiceSize(virtualMachineID string) error {
	data, err := json.Marshal(specification.MetadataServiceData(virtualMachineID, "255.255.255.255", "ff:ff:ff:ff:ff:ff"))
	if err != nil {
		return err
	}
	if len(data) > MetadataServiceSizeLimitBytes-metadataServiceReserveBytes {
		return ErrMetadataServiceTooLarge
	}
	return nil
}
