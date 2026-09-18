/* SPDX-License-Identifier: AGPL-3.0 */
/* TC ingress hook for decrypted WireGuard traffic. */
#ifndef ATLAS_WIREGUARD_HOOK_H
#define ATLAS_WIREGUARD_HOOK_H

#include "debug.h"
#include "state.h"

/*
 * Attached to TC ingress on wg0, after WireGuard has decrypted the packet.
 *
 * Atlas WG Mesh tunnel packets for a VM on this host have their outer IPv6
 * header removed, so Linux routes the original packet to the VM interface. A
 * tunnel for a VM that this host does not own is stale sender state; it is
 * dropped, and NDP refreshes the sender on its next neighbour exchange. Other
 * WireGuard traffic is left for normal Linux routing.
 */
SEC("tc")
int handle_wireguard_packet(struct __sk_buff *packet)
{
	void *data = (void *)(long)packet->data;
	void *end = (void *)(long)packet->data_end;
	struct ipv6hdr *outer = data;
	struct ipv6hdr *inner;
	struct in6_addr destination_vm;
	struct config *local_config;

	if (packet->protocol != bpf_htons(ETH_P_IPV6) || (void *)(outer + 1) > end) return TC_ACT_OK;
	local_config = get_config();
	if (!local_config) return TC_ACT_SHOT;

	/* Atlas tunnels always use host WireGuard addresses as their outer endpoints. */
	if (!is_underlay_address(&outer->saddr) || !are_ipv6_addresses_equal(&outer->daddr, &local_config->wg_ip6)) return TC_ACT_OK;

	/* Non-Atlas WG Mesh packets received through WireGuard continue normally. */
	if (outer->nexthdr != IPPROTO_IPV6) return TC_ACT_OK;
	inner = (void *)(outer + 1);
	if ((void *)(inner + 1) > end) return TC_ACT_SHOT;
	destination_vm = inner->daddr;
	if (!is_virtual_machine_address(&inner->saddr) || !is_virtual_machine_address(&destination_vm) || !tenants_can_communicate(&inner->saddr, &destination_vm)) return TC_ACT_SHOT;

	if (!is_local_virtual_machine(&destination_vm))
	{
		emit_packet_debug_event(DEBUG_WIREGUARD, DEBUG_DROP, &inner->saddr, &destination_vm);
		return TC_ACT_SHOT;
	}

	/*
		 * Remove the Atlas WG Mesh outer header. The host route selects the VM interface.
		 *
		 * BPF_F_ADJ_ROOM_FIXED_GSO keeps the segment size unchanged. GRO can
		 * join tunnel packets on wg0. Without this flag, the kernel adds 40
		 * bytes to each segment. The 1380-byte VM interface then rejects the 1420-byte
		 * segment.
		 */
	emit_packet_debug_event(DEBUG_WIREGUARD, DEBUG_ACCEPT, &inner->saddr, &inner->daddr);
	if (bpf_skb_adjust_room(packet, -(int)sizeof(struct ipv6hdr), BPF_ADJ_ROOM_MAC, BPF_F_ADJ_ROOM_NO_CSUM_RESET | BPF_F_ADJ_ROOM_FIXED_GSO)) return TC_ACT_SHOT;
	return TC_ACT_OK;
}

#endif /* ATLAS_WIREGUARD_HOOK_H */
