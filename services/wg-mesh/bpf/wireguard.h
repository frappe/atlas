/* SPDX-License-Identifier: AGPL-3.0 */
/* TC ingress on wg0, after decryption: deliver tunnels, and repair stale locations with NOT_HERE. */
#ifndef ATLAS_WIREGUARD_H
#define ATLAS_WIREGUARD_H

#include "maps.h"

/* Override, without solicited: an unsolicited advertisement replaces the MAC that a router holds. */
#define UNSOLICITED_ADVERTISEMENT_FLAGS 0x20000000u

/*
 * Remove the outer header. Linux routes the inner packet to the VM.
 *
 * GRO can join tunnel packets on wg0. BPF_F_ADJ_ROOM_FIXED_GSO keeps the segment size, or the 1380-byte VM interface rejects a 1420-byte segment.
 */
static __always_inline int remove_mesh_tunnel(struct __sk_buff *packet)
{
	if (bpf_skb_adjust_room(packet, -(int)sizeof(struct ipv6hdr), BPF_ADJ_ROOM_MAC, BPF_F_ADJ_ROOM_NO_CSUM_RESET | BPF_F_ADJ_ROOM_FIXED_GSO))
		return TC_ACT_SHOT;

	return TC_ACT_OK;
}

/* Replace a tunnel for a VM that is not here with NOT_HERE to its sender. WireGuard encrypts it back to that sender. */
static __always_inline int reply_not_here(struct __sk_buff *packet, struct config *config, const struct in6_addr *virtual_machine, const struct in6_addr *sender)
{
	struct ipv6hdr ip6 = {
		.version = 6,
		.payload_len = bpf_htons(sizeof(*virtual_machine)),
		.nexthdr = NOT_HERE_NEXT_HEADER,
		.hop_limit = HOP_LIMIT,
		.saddr = config->wireguard_ipv6,
		.daddr = *sender,
	};

	if (bpf_skb_change_tail(packet, NOT_HERE_PACKET_LENGTH, 0) ||
		bpf_skb_store_bytes(packet, 0, &ip6, sizeof(ip6), BPF_F_INVALIDATE_HASH) ||
		bpf_skb_store_bytes(packet, sizeof(ip6), virtual_machine, sizeof(*virtual_machine), BPF_F_INVALIDATE_HASH))
		return TC_ACT_SHOT;

	return bpf_redirect(packet->ifindex, 0);
}

/* Forget a location, but only when the host that holds it sends NOT_HERE. Then ask for the new host at once. */
static __always_inline int handle_not_here(struct __sk_buff *packet, struct config *config, const struct in6_addr *sender)
{
	struct in6_addr virtual_machine;
	struct in6_addr *host;

	if (bpf_skb_load_bytes(packet, sizeof(struct ipv6hdr), &virtual_machine, sizeof(virtual_machine)))
		return TC_ACT_SHOT;

	host = bpf_map_lookup_elem(&remote_vms, &virtual_machine);
	if (!host || !is_same_address(host, sender))
		return TC_ACT_SHOT;

	bpf_map_delete_elem(&remote_vms, &virtual_machine);

	/* wg0 has no link layer, so add room for the Ethernet header. */
	if (bpf_skb_change_head(packet, ETH_HLEN, 0) || replace_with_neighbor_solicitation(packet, config->uplink_mac, &config->uplink_ipv6, &virtual_machine))
		return TC_ACT_SHOT;

	return bpf_redirect(config->uplink_ifindex, 0);
}

/* The old host of a block forwards its traffic here. Advertise the address once, so the provider router sends the next packet to this host. */
static __always_inline int receive_moved_prefix_packet(struct __sk_buff *packet, struct config *config, const struct in6_addr *address)
{
	__u8 all_nodes_mac[ETH_ALEN] = {0x33, 0x33, 0, 0, 0, 1};
	struct in6_addr all_nodes = {.s6_addr = {0xff, 0x02, [15] = 1}};

	if (!take_announcement_token(address))
		return remove_mesh_tunnel(packet);

	/* wg0 has no link layer, so add room for the Ethernet header. */
	if (bpf_skb_change_head(packet, ETH_HLEN, 0) ||
		replace_with_public_advertisement(packet, config, all_nodes_mac, &all_nodes, address, UNSOLICITED_ADVERTISEMENT_FLAGS))
		return TC_ACT_SHOT;

	return bpf_redirect(config->public_ifindex, 0);
}

/* A client packet from a remote VM goes to the local gateway VM that the tunnel names. */
static __always_inline int deliver_to_gateway(struct __sk_buff *packet, struct config *config, const struct in6_addr *sender, struct ipv6hdr *outer, void *end)
{
	struct in6_addr *named_gateway = (void *)(outer + 1);
	struct ipv6hdr *inner = (void *)(named_gateway + 1);
	struct ethhdr eth = {.h_proto = bpf_htons(ETH_P_IPV6)};
	struct bpf_redir_neigh next_hop = {.nh_family = AF_INET6};
	struct in6_addr gateway, source, destination;
	__u32 ifindex;

	if ((void *)(inner + 1) > end ||
		sizeof(gateway) + sizeof(*inner) + bpf_ntohs(inner->payload_len) > bpf_ntohs(outer->payload_len))
		return TC_ACT_SHOT;

	gateway = *named_gateway;
	source = inner->saddr;
	destination = inner->daddr;
	if (!is_local_vm(&gateway))
		return reply_not_here(packet, config, &gateway, sender);

	ifindex = get_gateway_interface(&gateway);
	if (!ifindex || !is_vm_address(&source) || !is_gateway_carried_address(&destination))
		return TC_ACT_SHOT;

	__builtin_memcpy(next_hop.ipv6_nh, &gateway, sizeof(next_hop.ipv6_nh));

	/* wg0 has no link layer. The neighbor redirect needs an Ethernet header to fill. */
	if (bpf_skb_adjust_room(packet, -(int)(sizeof(*outer) + sizeof(gateway)), BPF_ADJ_ROOM_MAC, BPF_F_ADJ_ROOM_NO_CSUM_RESET | BPF_F_ADJ_ROOM_FIXED_GSO) ||
		bpf_skb_change_head(packet, sizeof(eth), 0) || bpf_skb_store_bytes(packet, 0, &eth, sizeof(eth), 0))
		return TC_ACT_SHOT;

	return bpf_redirect_neigh(ifindex, &next_hop, sizeof(next_hop), 0);
}

SEC("tc")
int handle_wireguard_packet(struct __sk_buff *packet)
{
	void *end = (void *)(long)packet->data_end;
	struct ipv6hdr *outer = (void *)(long)packet->data;
	struct ipv6hdr *inner = outer + 1;
	struct in6_addr sender, source, destination;
	struct config *config = get_config();

	if (packet->protocol != bpf_htons(ETH_P_IPV6) || (void *)(outer + 1) > end)
		return TC_ACT_OK;
	if (outer->version != 6 || sizeof(*outer) + bpf_ntohs(outer->payload_len) > packet->len)
		return TC_ACT_SHOT;

	if (!config)
		return TC_ACT_SHOT;

	/* Mesh tunnels run between host WireGuard addresses. Other WireGuard traffic is not ours. */
	if (!is_underlay_address(&outer->saddr) || !is_same_address(&outer->daddr, &config->wireguard_ipv6))
		return TC_ACT_OK;

	sender = outer->saddr;

	if (outer->nexthdr == NOT_HERE_NEXT_HEADER)
		return bpf_ntohs(outer->payload_len) == sizeof(struct in6_addr) ? handle_not_here(packet, config, &sender) : TC_ACT_SHOT;

	if (outer->nexthdr == GATEWAY_NEXT_HEADER)
		return deliver_to_gateway(packet, config, &sender, outer, end);

	if (outer->nexthdr != IPPROTO_IPV6)
		return TC_ACT_OK;

	if ((void *)(inner + 1) > end)
		return TC_ACT_SHOT;
	if (sizeof(*inner) + bpf_ntohs(inner->payload_len) > bpf_ntohs(outer->payload_len))
		return TC_ACT_SHOT;

	source = inner->saddr;
	destination = inner->daddr;

	/* A host sends its own traffic to a VM in a tunnel. WireGuard proved the sender. */
	if (is_same_address(&source, &sender) && is_vm_address(&destination))
		return is_local_vm(&destination) ? remove_mesh_tunnel(packet) : reply_not_here(packet, config, &destination, &sender);

	/* The sending host checked that the VM answers this host. */
	if (is_same_address(&destination, &config->wireguard_ipv6))
		return is_vm_address(&source) ? remove_mesh_tunnel(packet) : TC_ACT_SHOT;

	/* Only an old host sends a client packet for a local block. Gateway traffic uses its own tunnel. */
	if (!is_vm_address(&destination))
		return !is_vm_address(&source) && get_prefix_owner(&destination) ? receive_moved_prefix_packet(packet, config, &destination) : TC_ACT_SHOT;

	/* The sending host checked source ownership. A foreign source comes from its gateway. */
	if (is_vm_address(&source) && !can_communicate(&source, &destination))
		return TC_ACT_SHOT;

	if (!is_vm_address(&source) && !is_gateway_carried_address(&source))
		return TC_ACT_SHOT;

	if (!is_local_vm(&destination))
		return reply_not_here(packet, config, &destination, &sender);

	if (!is_vm_address(&source) && !has_gateway_return_route(&destination, &source))
		return TC_ACT_SHOT;

	return remove_mesh_tunnel(packet);
}

#endif /* ATLAS_WIREGUARD_H */
