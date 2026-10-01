package api

import (
	"fmt"
	"math"
	"net/netip"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/frappe/atlas/metal/internal/vm"
)

// Bounds on caller-supplied values.
const (
	// maximumResourceIDLength keeps an ID usable as a path, ZFS dataset, and
	// systemd unit instance name.
	maximumResourceIDLength = 64
	minimumCPUMillicores    = 100
	maximumCPUMillicores    = 32_000

	// maximumMemoryMiB prevents unit memory overflow. The unit limit is twice the
	// guest size plus fixed overhead.
	maximumMemoryMiB             = (math.MaxInt - 128) / 2
	maximumSleepAfterIdleSeconds = int64(math.MaxInt64) / int64(time.Second)
	maximumFirewallPrefixes      = 50

	maximumHostnameLength = 253
	maximumUserDataLength = 16 * 1024
)

// Patterns that keep caller-supplied values usable as host paths and names.
var (
	imageReferencePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]*$`)
	resourceIDPattern     = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*$`)
	sha256DigestPattern   = regexp.MustCompile(`^[a-fA-F0-9]{64}$`)
	firewallPortsPattern  = regexp.MustCompile(`^[0-9]+(?:-[0-9]+)?$`)

	// wireGuardMeshPrefix is the only block a VM mesh address may fall in.
	wireGuardMeshPrefix = netip.MustParsePrefix("fdaa::/16")
)

// createRequest is the complete desired specification of a new VM. Each group
// is required because creation stores state instead of merging it.
type createRequest struct {
	Compute computeRequest `json:"compute"`
	Disk    diskRequest    `json:"disk"`
	Image   imageRequest   `json:"image"`
	Network networkRequest `json:"network"`
	Guest   guestRequest   `json:"guest"`
}

// computeRequest is the complete compute configuration.
type computeRequest struct {
	CPUMillicores         int `json:"cpu_millicores" minimum:"100" maximum:"32000"`
	MemoryMiB             int `json:"memory_mib" minimum:"1"`
	SleepAfterIdleSeconds int `json:"sleep_after_idle_seconds" minimum:"0"`
}

// imageRequest identifies boot content and how the host should keep it.
type imageRequest struct {
	Ref                         string                              `json:"ref"`
	Architecture                string                              `json:"architecture"`
	Rootfs                      imageArtifactRequest                `json:"rootfs"`
	Kernel                      imageArtifactRequest                `json:"kernel"`
	CacheImage                  bool                                `json:"cache_image"`
	MemorySnapshot              bool                                `json:"memory_snapshot"`
	MemorySnapshotConfiguration *memorySnapshotConfigurationRequest `json:"memory_snapshot_configuration,omitempty"`
}

// memorySnapshotConfigurationRequest is the exact VM shape a warm image serves.
type memorySnapshotConfigurationRequest struct {
	VirtualCPUCount int `json:"virtual_cpu_count"`
	MemoryMiB       int `json:"memory_mib"`
	DiskMiB         int `json:"disk_mib"`
}

// imageArtifactRequest is one downloadable artifact and its digest.
type imageArtifactRequest struct {
	URL    string `json:"url"`
	SHA256 string `json:"sha256"`
}

// diskRequest is the complete disk size and its rate limits.
type diskRequest struct {
	SizeMiB         int `json:"size_mib" minimum:"1"`
	ThroughputMiBps int `json:"throughput_mibps"`
	IOPS            int `json:"iops"`
}

// validate rejects a disk that is not usable.
func (request diskRequest) validate() error {
	if request.SizeMiB <= 0 {
		return fmt.Errorf("disk.size_mib must be positive")
	}
	if request.ThroughputMiBps < 0 || request.IOPS < 0 {
		return fmt.Errorf("disk rate limits must not be negative")
	}
	return nil
}

// specification converts the request into the domain disk limits.
func (request diskRequest) specification() vm.Disk {
	return vm.Disk{ThroughputMiBps: request.ThroughputMiBps, IOPS: request.IOPS}
}

// networkRequest is the complete desired VM network.
type networkRequest struct {
	PublicIPv4                    string           `json:"public_ipv4"`
	Routes                        []routeRequest   `json:"routes"`
	IsNetworkGateway              bool             `json:"is_network_gateway"`
	PublicIPv6                    string           `json:"public_ipv6"`
	WireGuardMeshIPv6             string           `json:"wireguard_mesh_ipv6"`
	PrivateNetworkThroughputMiBps int              `json:"private_network_throughput_mibps"`
	PublicNetworkThroughputMiBps  int              `json:"public_network_throughput_mibps"`
	Firewall                      *firewallRequest `json:"firewall"`
}

type routeRequest struct {
	Destination string `json:"destination"`
	Via         string `json:"via" example:"host"`
	// Scope keeps a route in the VM namespace. Only the WireGuard gateway scope exists.
	Scope string `json:"scope,omitempty" example:"wireguard-gateway"`
}

// Only the host can carry IPv4 because gateway VMs use the IPv6 mesh.
func (route routeRequest) validate() error {
	if route.Scope != "" && route.Scope != vm.RouteScopeWireGuardGateway {
		return fmt.Errorf(
			"network.routes scope %q must be empty or %q", route.Scope, vm.RouteScopeWireGuardGateway,
		)
	}
	destination, err := netip.ParsePrefix(route.Destination)
	if err != nil || destination.Masked().String() != route.Destination || destination.Addr().Is4In6() {
		return fmt.Errorf("network.routes destination %q must be a canonical IP prefix", route.Destination)
	}
	if route.Via == vm.RouteViaHost {
		return nil
	}

	gateway, err := netip.ParseAddr(route.Via)
	if err != nil || !wireGuardMeshPrefix.Contains(gateway) {
		return fmt.Errorf("network.routes via must be %q or an address in %s", vm.RouteViaHost, wireGuardMeshPrefix)
	}
	if destination.Addr().Is4() {
		return fmt.Errorf("network.routes destination %s must use via %q", route.Destination, vm.RouteViaHost)
	}
	return nil
}

func toRouteSpecifications(routes []routeRequest) []vm.Route {
	specifications := make([]vm.Route, len(routes))
	for index, route := range routes {
		specifications[index] = vm.Route{Destination: route.Destination, Via: route.Via, Scope: route.Scope}
	}
	return specifications
}

// firewallRequest is the complete desired firewall configuration.
type firewallRequest struct {
	Enabled  bool                  `json:"enabled"`
	Inbound  []firewallRuleRequest `json:"inbound"`
	Outbound []firewallRuleRequest `json:"outbound"`
}

// firewallRuleRequest permits traffic from or to a set of IP prefixes.
type firewallRuleRequest struct {
	Protocol string   `json:"protocol" enums:"any,tcp,udp,icmp"`
	Ports    string   `json:"ports,omitempty"`
	CIDRs    []string `json:"cidrs"`
}

// guestRequest carries the values published to the guest through MMDS.
type guestRequest struct {
	Hostname string            `json:"hostname"`
	SSHKeys  []string          `json:"ssh_keys"`
	Metadata map[string]string `json:"metadata"`
	UserData string            `json:"user_data"`
}

// powerRequest is the desired power state.
type powerRequest struct {
	State string `json:"state" enums:"running,stopped,paused"`
}

// validate checks every group and the guest values a create carries.
func (request createRequest) validate() error {
	if err := request.Compute.validate(); err != nil {
		return err
	}
	if err := request.Disk.validate(); err != nil {
		return err
	}
	if err := request.Image.validate(); err != nil {
		return err
	}
	if err := request.Network.validate(); err != nil {
		return err
	}

	return request.Guest.validate()
}

func (request guestRequest) validate() error {
	if len(request.Hostname) > maximumHostnameLength {
		return fmt.Errorf("guest.hostname is too long")
	}
	if len(request.UserData) > maximumUserDataLength {
		return fmt.Errorf("guest.user_data must not exceed %d bytes", maximumUserDataLength)
	}
	if _, err := validateSSHKeys(request.SSHKeys); err != nil {
		return err
	}
	return validateMetadata(request.Metadata)
}

// specification converts the request into the domain VM specification.
func (request createRequest) specification() vm.Specification {
	return vm.Specification{
		CPUMillicores:         request.Compute.CPUMillicores,
		MemoryMiB:             request.Compute.MemoryMiB,
		SleepAfterIdleSeconds: request.Compute.SleepAfterIdleSeconds,
		DiskMiB:               request.Disk.SizeMiB,
		Disk:                  request.Disk.specification(),
		Image:                 request.Image.specification(),
		Network:               request.Network.specification(),
		SSHKeys:               request.Guest.SSHKeys,
		Hostname:              request.Guest.Hostname,
		UserData:              request.Guest.UserData,
		Metadata:              request.Guest.Metadata,
	}
}

// validate rejects a compute shape the host cannot represent.
func (request computeRequest) validate() error {
	if request.CPUMillicores < minimumCPUMillicores {
		return fmt.Errorf("compute.cpu_millicores must be at least %d", minimumCPUMillicores)
	}
	if request.CPUMillicores > maximumCPUMillicores {
		return fmt.Errorf("compute.cpu_millicores must not exceed %d", maximumCPUMillicores)
	}
	if request.MemoryMiB <= 0 {
		return fmt.Errorf("compute.memory_mib must be positive")
	}
	if request.MemoryMiB > maximumMemoryMiB {
		return fmt.Errorf("compute.memory_mib is too large")
	}
	if request.SleepAfterIdleSeconds < 0 {
		return fmt.Errorf("compute.sleep_after_idle_seconds must not be negative")
	}
	if int64(request.SleepAfterIdleSeconds) > maximumSleepAfterIdleSeconds {
		return fmt.Errorf("compute.sleep_after_idle_seconds is too large")
	}
	return nil
}

// specification converts the request into the domain image.
func (request imageRequest) specification() vm.Image {
	return vm.Image{
		Name:                        request.Ref,
		Architecture:                request.Architecture,
		RootfsURL:                   request.Rootfs.URL,
		RootfsSHA256:                request.Rootfs.SHA256,
		KernelURL:                   request.Kernel.URL,
		KernelSHA256:                request.Kernel.SHA256,
		CacheImage:                  request.CacheImage,
		MemorySnapshot:              request.MemorySnapshot,
		MemorySnapshotConfiguration: request.MemorySnapshotConfiguration.specification(),
	}
}

// validate rejects an image that cannot be fetched or verified.
func (request imageRequest) validate() error {
	if !validImageReference(request.Ref) {
		return fmt.Errorf("image.ref must match [A-Za-z0-9._:-] and start alphanumeric")
	}
	if request.Architecture == "" {
		return fmt.Errorf("image.architecture is required")
	}
	if !validHTTPURL(request.Rootfs.URL) || !validHTTPURL(request.Kernel.URL) {
		return fmt.Errorf("image rootfs and kernel URLs must use HTTP or HTTPS")
	}
	if !validSHA256Digest(request.Rootfs.SHA256) || !validSHA256Digest(request.Kernel.SHA256) {
		return fmt.Errorf("image rootfs and kernel SHA-256 values are invalid")
	}
	if request.MemorySnapshot {
		configuration := request.MemorySnapshotConfiguration
		if configuration == nil ||
			configuration.VirtualCPUCount <= 0 ||
			configuration.VirtualCPUCount > maximumCPUMillicores/1000 ||
			configuration.MemoryMiB <= 0 ||
			configuration.DiskMiB <= 0 {
			return fmt.Errorf("image.memory_snapshot_configuration has invalid values")
		}
	}

	return nil
}

// specification converts the request into the domain warm image shape.
func (request *memorySnapshotConfigurationRequest) specification() *vm.MemorySnapshotConfiguration {
	if request == nil {
		return nil
	}

	return &vm.MemorySnapshotConfiguration{
		VirtualCPUCount: request.VirtualCPUCount,
		MemoryMiB:       request.MemoryMiB,
		DiskMiB:         request.DiskMiB,
	}
}

// validate rejects a network the host cannot build.
func (request networkRequest) validate() error {
	if request.PrivateNetworkThroughputMiBps < 0 || request.PublicNetworkThroughputMiBps < 0 {
		return fmt.Errorf("network throughput values must not be negative")
	}

	wireGuardAddress, err := netip.ParseAddr(request.WireGuardMeshIPv6)
	if err != nil || !wireGuardMeshPrefix.Contains(wireGuardAddress) {
		return fmt.Errorf("network.wireguard_mesh_ipv6 must be in fdaa::/16")
	}
	if request.Firewall == nil {
		return fmt.Errorf("network.firewall is required")
	}
	if err := request.Firewall.validate(); err != nil {
		return err
	}
	if err := request.validateRoutes(); err != nil {
		return err
	}
	return request.validatePublicAddresses()
}

func (request networkRequest) validateRoutes() error {
	destinations := make(map[string]bool, len(request.Routes))
	for _, route := range request.Routes {
		if err := route.validate(); err != nil {
			return err
		}
		if destinations[route.Destination] {
			return fmt.Errorf("network.routes destination %s is listed twice", route.Destination)
		}
		destinations[route.Destination] = true
	}
	return nil
}

// validatePublicAddresses requires a host route for each public address,
// because replies to a public address leave through the host uplink.
func (request networkRequest) validatePublicAddresses() error {
	configuration := vm.NetworkConfiguration{Routes: toRouteSpecifications(request.Routes)}
	if request.PublicIPv4 != "" {
		publicAddress, err := netip.ParseAddr(request.PublicIPv4)
		if err != nil || !publicAddress.Is4() {
			return fmt.Errorf("network.public_ipv4 must be an IPv4 address")
		}
		if !configuration.HasIPv4HostRoute() {
			return fmt.Errorf("network.public_ipv4 requires an IPv4 route via %q", vm.RouteViaHost)
		}
	}
	if request.PublicIPv6 == "" {
		return nil
	}
	if !isCanonicalIPv6Prefix(request.PublicIPv6) {
		return fmt.Errorf("network.public_ipv6 %q must be a canonical IPv6 prefix", request.PublicIPv6)
	}
	if !configuration.HasIPv6HostRoute() {
		return fmt.Errorf("network.public_ipv6 requires an IPv6 route via %q", vm.RouteViaHost)
	}
	return nil
}

// isCanonicalIPv6Prefix accepts a prefix only in the text form that ip prints,
// so a reconcile pass can compare the host routes as text.
func isCanonicalIPv6Prefix(value string) bool {
	prefix, err := netip.ParsePrefix(value)
	return err == nil && prefix.Addr().Is6() && !prefix.Addr().Is4In6() && prefix.Masked().String() == value
}

// specification converts the request into the domain network configuration.
func (request networkRequest) specification() vm.NetworkConfiguration {
	return vm.NetworkConfiguration{
		PublicIPv4:                    request.PublicIPv4,
		WireGuardMeshIPv6:             request.WireGuardMeshIPv6,
		Routes:                        toRouteSpecifications(request.Routes),
		IsNetworkGateway:              request.IsNetworkGateway,
		PublicIPv6:                    request.PublicIPv6,
		PrivateNetworkThroughputMiBps: request.PrivateNetworkThroughputMiBps,
		PublicNetworkThroughputMiBps:  request.PublicNetworkThroughputMiBps,
		Firewall:                      request.Firewall.specification(),
	}
}

// validate rejects firewall values that cannot be represented by iptables.
func (request firewallRequest) validate() error {
	inboundPrefixes, err := validateFirewallRules("inbound", request.Inbound)
	if err != nil {
		return err
	}
	outboundPrefixes, err := validateFirewallRules("outbound", request.Outbound)
	if err != nil {
		return err
	}
	prefixes := inboundPrefixes + outboundPrefixes
	if prefixes > maximumFirewallPrefixes {
		return fmt.Errorf("network.firewall must not exceed %d prefix entries", maximumFirewallPrefixes)
	}
	return nil
}

func validateFirewallRules(direction string, rules []firewallRuleRequest) (int, error) {
	prefixes := 0
	for index, rule := range rules {
		if err := rule.validate(); err != nil {
			return 0, fmt.Errorf("network.firewall.%s[%d]: %w", direction, index, err)
		}
		prefixes += len(rule.CIDRs)
	}
	return prefixes, nil
}

func (request firewallRuleRequest) validate() error {
	protocol := vm.FirewallProtocol(request.Protocol)
	if !protocol.IsValid() {
		return fmt.Errorf("protocol must be any, tcp, udp, or icmp")
	}
	if err := validateFirewallPorts(protocol, request.Ports); err != nil {
		return err
	}
	if len(request.CIDRs) == 0 {
		return fmt.Errorf("cidrs must contain at least one prefix")
	}
	for _, value := range request.CIDRs {
		prefix, err := netip.ParsePrefix(value)
		if err != nil || prefix != prefix.Masked() {
			return fmt.Errorf("cidr %q must be a canonical IPv4 or IPv6 prefix", value)
		}
	}
	return nil
}

func validateFirewallPorts(protocol vm.FirewallProtocol, ports string) error {
	if ports == "" {
		return nil
	}
	if protocol != vm.FirewallProtocolTCP && protocol != vm.FirewallProtocolUDP {
		return fmt.Errorf("ports are valid only for tcp or udp")
	}
	if !firewallPortsPattern.MatchString(ports) {
		return fmt.Errorf("ports must be one port or one inclusive range")
	}

	startValue, endValue, hasRange := strings.Cut(ports, "-")
	start, _ := strconv.Atoi(startValue)
	end := start
	if hasRange {
		end, _ = strconv.Atoi(endValue)
	}
	if strconv.Itoa(start) != startValue || (hasRange && strconv.Itoa(end) != endValue) {
		return fmt.Errorf("ports must not contain leading zeros")
	}
	if start < 1 || end > 65535 || start > end {
		return fmt.Errorf("ports must be between 1 and 65535 in ascending order")
	}
	return nil
}

func (request firewallRequest) specification() vm.FirewallConfiguration {
	return vm.FirewallConfiguration{
		Enabled:  request.Enabled,
		Inbound:  firewallRuleSpecifications(request.Inbound),
		Outbound: firewallRuleSpecifications(request.Outbound),
	}
}

func firewallRuleSpecifications(requests []firewallRuleRequest) []vm.FirewallRule {
	rules := make([]vm.FirewallRule, len(requests))
	for index, request := range requests {
		rules[index] = vm.FirewallRule{
			Protocol: vm.FirewallProtocol(request.Protocol),
			Ports:    request.Ports,
			CIDRs:    append([]string(nil), request.CIDRs...),
		}
	}
	return rules
}

// state returns the requested power state, or an error for any other value.
func (request powerRequest) state() (vm.State, error) {
	state := vm.State(request.State)
	if state != vm.StateRunning && state != vm.StateStopped && state != vm.StatePaused {
		return "", fmt.Errorf("state must be running, stopped, or paused")
	}
	return state, nil
}

// validHTTPURL reports whether value is an absolute http or https URL.
func validHTTPURL(value string) bool {
	parsed, err := url.ParseRequestURI(value)
	return err == nil && (parsed.Scheme == "http" || parsed.Scheme == "https") && parsed.Host != ""
}

// validImageReference reports whether value is usable as a directory and ZFS
// dataset name.
func validImageReference(value string) bool {
	return imageReferencePattern.MatchString(value)
}

// validResourceID reports whether value is usable as a VM or snapshot identifier.
func validResourceID(value string) bool {
	return len(value) <= maximumResourceIDLength && resourceIDPattern.MatchString(value)
}

// validSHA256Digest reports whether value is a full hexadecimal SHA-256 digest.
func validSHA256Digest(value string) bool {
	return sha256DigestPattern.MatchString(value)
}
