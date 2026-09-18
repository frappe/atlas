/* SPDX-License-Identifier: AGPL-3.0 */
/* Atlas WG Mesh state shared by the TC programs and the integration. */
#ifndef ATLAS_STATE_H
#define ATLAS_STATE_H

#include "address.h"

/* VMs connected to this host. The value is the ifindex of the interface that owns
 * the VM, so one VM cannot send traffic with another VM's source address. */
struct
{
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, struct in6_addr);
	__type(value, __u32);
	__uint(max_entries, 4096);
} local_vms SEC(".maps");

/* Privileged-tenant addresses allowed to communicate with other tenants. The
 * controller keeps this whitelist in sync on every host. */
struct
{
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, struct in6_addr);
	__type(value, __u8);
	__uint(max_entries, 4096);
} privileged_tenant_allowed_addresses SEC(".maps");

/* Learned remote VM-to-WireGuard-host locations, filled from NDP advertisements. */
struct
{
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__type(key, struct in6_addr);
	__type(value, struct in6_addr);
	__uint(max_entries, 262144);
} remote_vms SEC(".maps");

/* Consecutive NUD failures for remote VMs.
 *
 * The NUD hook increments this counter on each NUD_FAILED event.
 * A NUD_REACHABLE event resets it to zero.
 * The NUD hook removes the entry once the failure count reaches
 * ATLAS_NUD_FAILURE_LIMIT.
 */
struct
{
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__type(key, struct in6_addr);
	__type(value, __u32);
	__uint(max_entries, 262144);
} nud_failures SEC(".maps");

/* Peer capacity of the peer_list map. The unicast hooks use the same limit to
 * bound their loops. */
#define ATLAS_UNICAST_PEER_LIMIT 256

/* Peer IPv4 addresses for unicast NDP transport. The unicast daemon fills this
 * map from the peer file on disk. Peers occupy the low indexes densely; the
 * first zero value ends the list. */
struct
{
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__type(key, __u32);
	__type(value, __be32);
	__uint(max_entries, ATLAS_UNICAST_PEER_LIMIT);
} peer_list SEC(".maps");

/* Last known owner peer for each remote VM. The unicast egress hook sends one
 * solicitation to that peer and removes the entry. A missing answer therefore
 * makes the next solicitation fan out to every peer. */
struct
{
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__type(key, struct in6_addr);
	__type(value, __be32);
	__uint(max_entries, 262144);
} vm_peer_map SEC(".maps");

/* Peers that asked this host about a VM. The key is the requester address from
 * a transported solicitation, and the value is the IPv4 address from the
 * requester option. The unicast egress hook wraps the answer to that peer. */
struct
{
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__type(key, struct in6_addr);
	__type(value, __be32);
	__uint(max_entries, 4096);
} ndp_requesters SEC(".maps");

/* Host configuration. The discovery index records the configured uplink, and
 * the underlay IPv4 address also sources the unicast NDP transport. */
struct config
{
	__u32 discovery_ifindex;
	__be32 underlay_ip4;
	struct in6_addr wg_ip6;
	__u8 discovery_mac[ETH_ALEN];
};

/* One host configuration entry. The integration writes it during setup. */
struct
{
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__type(key, __u32);
	__type(value, struct config);
	__uint(max_entries, 1);
} config SEC(".maps");

struct
{
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__type(key, __u32);
	__type(value, __u8[32]);
	__uint(max_entries, 1);
} build_hash SEC(".maps");

/* Get the current host configuration. */
static __always_inline struct config *get_config(void)
{
	__u32 key = 0;

	return bpf_map_lookup_elem(&config, &key);
}

/* Check if the given address is a local VM. */
static __always_inline int is_local_virtual_machine(const struct in6_addr *virtual_machine)
{
	return bpf_map_lookup_elem(&local_vms, virtual_machine) != NULL;
}

/* True only when the VM is registered on this exact interface. A VM must not
 * send traffic with the source address of a VM on another interface. */
static __always_inline int owns_source_address(const struct in6_addr *virtual_machine, __u32 ifindex)
{
	__u32 *owner = bpf_map_lookup_elem(&local_vms, virtual_machine);

	return owner && *owner == ifindex;
}

/* Cross-tenant traffic is permitted only when one endpoint is a whitelisted
 * privileged-tenant address. This preserves request and response traffic. */
static __always_inline int tenants_can_communicate(const struct in6_addr *source, const struct in6_addr *destination)
{
	if (get_tenant(source) == get_tenant(destination)) return 1;

	if (get_tenant(source) == 0 && bpf_map_lookup_elem(&privileged_tenant_allowed_addresses, source) != NULL) return 1;

	return get_tenant(destination) == 0 && bpf_map_lookup_elem(&privileged_tenant_allowed_addresses, destination) != NULL;
}

/* Get the remote location (WireGuard address of the bare metal host) of the given VM. */
static __always_inline struct in6_addr *get_remote_location(const struct in6_addr *virtual_machine)
{
	return bpf_map_lookup_elem(&remote_vms, virtual_machine);
}

#endif /* ATLAS_STATE_H */
