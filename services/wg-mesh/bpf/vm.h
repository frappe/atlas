/* SPDX-License-Identifier: AGPL-3.0 */
/* TC ingress on every VM interface: enforce ownership and tenants, then tunnel or discover. */
#ifndef ATLAS_VM_H
#define ATLAS_VM_H

#include "maps.h"

/* Add the outer IPv6 header, and the gateway address for a tunnel to a gateway. Linux then routes the packet to wg0. */
static __always_inline int add_mesh_tunnel(struct __sk_buff *packet, struct config *config, const struct in6_addr *host, __u32 inner_length, const struct in6_addr *gateway)
{
	__u32 gateway_length = gateway ? sizeof(*gateway) : 0;
	struct ipv6hdr outer = {
		.version = 6,
		.payload_len = bpf_htons(inner_length + gateway_length),
		.nexthdr = gateway ? GATEWAY_NEXT_HEADER : IPPROTO_IPV6,
		.hop_limit = HOP_LIMIT,
		.saddr = config->wireguard_ipv6,
		.daddr = *host,
	};
	__u64 flags = BPF_F_ADJ_ROOM_FIXED_GSO | BPF_F_ADJ_ROOM_ENCAP_L3_IPV6 | BPF_F_ADJ_ROOM_NO_CSUM_RESET;

	if (inner_length + gateway_length > 0xffff ||
		bpf_skb_adjust_room(packet, sizeof(outer) + gateway_length, BPF_ADJ_ROOM_MAC, flags) ||
		bpf_skb_store_bytes(packet, ETH_HLEN, &outer, sizeof(outer), BPF_F_INVALIDATE_HASH))
		return TC_ACT_SHOT;
	if (gateway && bpf_skb_store_bytes(packet, ETH_HLEN + sizeof(outer), gateway, sizeof(*gateway), 0))
		return TC_ACT_SHOT;

	return TC_ACT_OK;
}

/* Replace the packet with a neighbor solicitation. The destination host answers through proxy NDP, and the uplink hook learns its location. */
static __always_inline int start_vm_discovery(struct __sk_buff *packet, struct config *config, const struct in6_addr *destination)
{
	if (!take_discovery_token(packet->ifindex))
		return TC_ACT_SHOT;

	if (replace_with_neighbor_solicitation(packet, config->uplink_mac, &config->uplink_ipv6, destination))
		return TC_ACT_SHOT;

	return bpf_redirect(config->uplink_ifindex, 0);
}

/* Tunnel to the host of a remote VM, or discover that host. A gateway tunnel names the gateway VM. */
static __always_inline int tunnel_to_remote_vm(struct __sk_buff *packet, const struct in6_addr *virtual_machine, __u32 inner_length, const struct in6_addr *gateway)
{
	struct config *config = get_config();
	struct in6_addr *host;

	if (!config)
		return TC_ACT_SHOT;

	host = bpf_map_lookup_elem(&remote_vms, virtual_machine);
	if (!host)
		return start_vm_discovery(packet, config, virtual_machine);

	return add_mesh_tunnel(packet, config, host, inner_length, gateway);
}

/* Send a packet for a destination outside the mesh to the gateway that this VM routes it to. */
static __always_inline int route_to_gateway(struct __sk_buff *packet, const struct in6_addr *source, const struct in6_addr *destination, __u32 inner_length)
{
	struct in6_addr *gateway = get_gateway_route(source, destination);
	struct bpf_redir_neigh next_hop = {.nh_family = AF_INET6};
	__u32 ifindex;

	/* Without a route the packet keeps normal host routing. */
	if (!gateway)
		return TC_ACT_OK;

	ifindex = get_gateway_interface(gateway);
	if (!ifindex)
		return tunnel_to_remote_vm(packet, gateway, inner_length, gateway);

	/* Linux would route a public destination out of the uplink, so hand the packet to the gateway VM directly. */
	__builtin_memcpy(next_hop.ipv6_nh, gateway, sizeof(next_hop.ipv6_nh));

	return bpf_redirect_neigh(ifindex, &next_hop, sizeof(next_hop), 0);
}

/* A VM sends from its own mesh address or from an address of its block. */
static __always_inline int owns_source(__u32 ifindex, const struct in6_addr *source)
{
	__u32 *owner = is_vm_address(source) ? bpf_map_lookup_elem(&local_vms, source) : get_prefix_owner(source);

	return owner && *owner == ifindex;
}

/* Only a gateway may send a foreign source into the mesh, because it has no tenant to check. */
static __always_inline int source_is_allowed(__u32 ifindex, const struct in6_addr *source, const struct in6_addr *destination)
{
	if (!is_vm_address(source))
		return is_gateway_interface(ifindex);

	if (!is_owned_by(source, ifindex))
		return 0;

	return can_communicate(source, destination);
}

SEC("tc")
int handle_vm_packet(struct __sk_buff *packet)
{
	void *end = (void *)(long)packet->data_end;
	struct ipv6hdr *ip6 = parse_ethernet_ipv6((void *)(long)packet->data, end);
	struct in6_addr source, destination;
	__u32 inner_length;

	if (!ip6)
		return TC_ACT_OK;

	source = ip6->saddr;
	destination = ip6->daddr;
	/* The outer header must describe the inner packet exactly, and its length field holds 16 bits. */
	inner_length = bpf_ntohs(ip6->payload_len) + sizeof(*ip6);
	if (inner_length > packet->len - ETH_HLEN || inner_length > 0xffff)
		return TC_ACT_SHOT;

	if (is_underlay_address(&destination))
		return TC_ACT_SHOT;

	if (!is_vm_address(&destination))
	{
		if (is_gateway_interface(packet->ifindex) || !is_gateway_carried_address(&destination))
			return TC_ACT_OK;

		/* A VM with its own block routes every destination through the host, so check the source here too. */
		if (!owns_source(packet->ifindex, &source))
			return TC_ACT_SHOT;

		return route_to_gateway(packet, &source, &destination, inner_length);
	}

	if (!source_is_allowed(packet->ifindex, &source, &destination))
		return TC_ACT_SHOT;

	if (is_controller(&destination))
		return is_vm_address(&source) ? TC_ACT_OK : TC_ACT_SHOT;

	/* Linux delivers same-host traffic through the VM host route. */
	if (is_local_vm(&destination))
		return is_vm_address(&source) || has_gateway_return_route(&destination, &source) ? TC_ACT_OK : TC_ACT_SHOT;

	return tunnel_to_remote_vm(packet, &destination, inner_length, NULL);
}

#endif /* ATLAS_VM_H */
