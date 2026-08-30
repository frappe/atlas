/* SPDX-License-Identifier: AGPL-3.0 */
/* TC ingress hook for every VM interface. */
#ifndef ATLAS_VM_HOOK_H
#define ATLAS_VM_HOOK_H

#include "control.h"
#include "discovery_limit.h"

static __always_inline int add_tunnel_header(
	struct __sk_buff *packet, struct config *local_config,
	const struct in6_addr *remote_host, __u16 inner_packet_length);

/*
 * Attached to TC ingress on every VM interface.
 *
 * It accepts only traffic from a registered local VM. Same-host VM traffic
 * stays with normal Linux routing. For a remote VM it uses a cached host or
 * turns the first packet into a WHO_HAS request; then it adds an IPv6 tunnel
 * header for WireGuard.
 */
SEC("tc")
int handle_vm_packet(struct __sk_buff *packet)
{
	void *data = (void *)(long)packet->data;
	void *end = (void *)(long)packet->data_end;
	struct ethhdr *eth = data;
	struct ipv6hdr *ip6;
	struct in6_addr src, dst;
	struct config *local_config;
	struct in6_addr *remote_host;
	__u16 inner_packet_length;

	if ((void *)(eth + 1) > end || eth->h_proto != bpf_htons(ETH_P_IPV6))
		return TC_ACT_OK;
	ip6 = (void *)(eth + 1);
	if ((void *)(ip6 + 1) > end)
		return TC_ACT_OK;

	src = ip6->saddr;
	dst = ip6->daddr;
	inner_packet_length = (__u16)(bpf_ntohs(ip6->payload_len) + sizeof(*ip6));

	/* The underlay carries every tenant's traffic. Guests stay off it. */
	if (is_underlay_address(&dst))
	{
		emit_packet_debug_event(DEBUG_VM, DEBUG_DROP, &src, &dst);
		return TC_ACT_SHOT;
	}

	/* Only fdaa::/16 traffic belongs to the mesh; preserve all other IPv6. */
	if (!is_virtual_machine_address(&dst))
	{
		emit_packet_debug_event(DEBUG_VM, DEBUG_ACCEPT, &src, &dst);
		return TC_ACT_OK;
	}

	if (!owns_source_address(&src, packet->ifindex))
	{
		emit_packet_debug_event(DEBUG_VM, DEBUG_DROP, &src, &dst);
		return TC_ACT_SHOT;
	}

	if (!tenants_can_communicate(&src, &dst))
	{
		emit_packet_debug_event(DEBUG_VM, DEBUG_DROP, &src, &dst);
		return TC_ACT_SHOT;
	}

	if (is_local_virtual_machine(&dst))
	{
		emit_packet_debug_event(DEBUG_VM, DEBUG_ACCEPT, &src, &dst);
		return TC_ACT_OK;
	}

	local_config = get_config();
	if (!local_config)
	{
		emit_packet_debug_event(DEBUG_VM, DEBUG_DROP, &src, &dst);
		return TC_ACT_SHOT;
	}

	remote_host = get_remote_location(&dst);
	/* The first unknown packet becomes WHO_HAS; the guest retry uses FOUND. */
	if (!remote_host)
	{
		/* Cap discovery so one guest cannot flood the multicast L2. */
		if (!discovery_allowed(local_config, &src))
		{
			emit_packet_debug_event(DEBUG_VM, DEBUG_DROP, &src, &dst);
			return TC_ACT_SHOT;
		}
		return ask_for_virtual_machine_host(packet, local_config, &dst);
	}

	emit_packet_debug_event(DEBUG_VM, DEBUG_REDIRECT, &src, &dst);
	return add_tunnel_header(packet, local_config, remote_host,
							 inner_packet_length);
}

/* Add the outer IPv6 header. The host then routes this packet through wg0. */
static __always_inline int add_tunnel_header(
	struct __sk_buff *packet, struct config *local_config,
	const struct in6_addr *remote_host, __u16 inner_packet_length)
{
	struct ipv6hdr outer = {};

	outer.version = 6;
	outer.payload_len = bpf_htons(inner_packet_length);
	outer.nexthdr = IPPROTO_IPV6;
	outer.hop_limit = 64;
	outer.saddr = local_config->wg_ip6;
	outer.daddr = *remote_host;

	/* Preserve the checksum state belonging to the inner guest packet. */
	if (bpf_skb_adjust_room(packet, sizeof(outer), BPF_ADJ_ROOM_MAC, BPF_F_ADJ_ROOM_ENCAP_L3_IPV6 | BPF_F_ADJ_ROOM_NO_CSUM_RESET))
		return TC_ACT_SHOT;

	if (bpf_skb_store_bytes(packet, ETH_HLEN, &outer, sizeof(outer), 0))
		return TC_ACT_SHOT;

	return TC_ACT_OK;
}

#endif /* ATLAS_VM_HOOK_H */
