/* SPDX-License-Identifier: AGPL-3.0 */
/* Atlas WG Mesh state shared by the three TC programs and the integration. */
#ifndef ATLAS_STATE_H
#define ATLAS_STATE_H

#include "protocol.h"

/* VMs connected to this host. The value is the ifindex of the interface that owns
 * the VM, so one VM cannot send traffic with another VM's source address. */
struct
{
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, struct in6_addr);
	__type(value, __u32);
	__uint(max_entries, 4096);
} local_vms SEC(".maps");

/* Learned remote VM-to-WireGuard-host locations. A miss sends WHO_HAS. */
struct
{
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__type(key, struct in6_addr);
	__type(value, struct in6_addr);
	__uint(max_entries, 262144);
} remote_vms SEC(".maps");

/* Host configuration. */
struct config
{
	/* Destination for locally generated WHO_HAS frames. */
	__u32 discovery_ifindex;
	__be32 underlay_ip4;
	__u8 uplink_mac[ETH_ALEN];
	__u8 pad[2];
	struct in6_addr wg_ip6; /* This host's WireGuard address. */
	__u32 who_has_rate;		/* Sustained WHO_HAS per second, per VM. 0 disables. */
	__u32 who_has_burst;	/* Discovery bucket capacity. */
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
static __always_inline int is_local_virtual_machine(
	const struct in6_addr *virtual_machine)
{
	return bpf_map_lookup_elem(&local_vms, virtual_machine) != NULL;
}

/* True only when the VM is registered on this exact interface. A VM must not
 * send traffic with the source address of a VM on another interface. */
static __always_inline int owns_source_address(
	const struct in6_addr *virtual_machine, __u32 ifindex)
{
	__u32 *owner = bpf_map_lookup_elem(&local_vms, virtual_machine);

	return owner && *owner == ifindex;
}

/* Get the remote location (WireGuard address of the bare metal host) of the given VM. */
static __always_inline struct in6_addr *get_remote_location(
	const struct in6_addr *virtual_machine)
{
	return bpf_map_lookup_elem(&remote_vms, virtual_machine);
}

#endif /* ATLAS_STATE_H */
