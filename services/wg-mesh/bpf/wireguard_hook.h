/* SPDX-License-Identifier: AGPL-3.0 */
/* TC ingress hook for decrypted WireGuard traffic. */
#ifndef ATLAS_WIREGUARD_HOOK_H
#define ATLAS_WIREGUARD_HOOK_H

#include "control.h"

/*
 * Attached to TC ingress on wg0, after WireGuard has decrypted the packet.
 *
 * Atlas WG Mesh tunnel packets for a VM on this host have their outer IPv6 header
 * removed, so Linux routes the original packet to the VM interface. If the VM has
 * moved away, this hook sends NOT_HERE to invalidate the sender's cache.
 * Other WireGuard traffic is left for normal Linux routing.
 */
SEC("tc")
int handle_wireguard_packet(struct __sk_buff *packet)
{
	void *data = (void *)(long)packet->data;
	void *end = (void *)(long)packet->data_end;
	struct ipv6hdr *outer = data;
	struct ipv6hdr *inner;
	struct atlas_msg msg;
	struct in6_addr sending_host, destination_vm;
	struct config *local_config;
	struct in6_addr *cached_host;

	if (packet->protocol != bpf_htons(ETH_P_IPV6) || (void *)(outer + 1) > end)
		return TC_ACT_OK;
	local_config = get_config();
	if (!local_config)
		return TC_ACT_SHOT;

	/* Atlas tunnels always use host WireGuard addresses as their outer endpoints. */
	if (!is_underlay_address(&outer->saddr) ||
		!are_ipv6_addresses_equal(&outer->daddr, &local_config->wg_ip6))
		return TC_ACT_OK;
	sending_host = outer->saddr;

	if (outer->nexthdr == ATLAS_CONTROL_NEXT_HEADER) {
		if (bpf_skb_load_bytes(packet, WIREGUARD_CONTROL_MESSAGE_OFFSET,
				       &msg, sizeof(msg)) ||
		    msg.ver != ATLAS_VER ||
		    msg.op != MESSAGE_OPERATION_NOT_HERE)
			return TC_ACT_SHOT;
		emit_protocol_debug_event(DEBUG_WIREGUARD, DEBUG_RECEIVE, msg.op, &msg.vm,
				   &sending_host);

		/* Only the cached host may invalidate a location it previously advertised. */
		cached_host = get_remote_location(&msg.vm);
		if (cached_host && are_ipv6_addresses_equal(cached_host, &sending_host)) {
			bpf_map_delete_elem(&remote_vms, &msg.vm);
			local_config = get_config();
			if (!local_config)
				return TC_ACT_SHOT;

			/* Start discovery now. Do not wait for the guest to retry. */
			return ask_for_virtual_machine_host(packet, local_config, &msg.vm);
		}
		return TC_ACT_SHOT;
	}

	/* Non-Atlas WG Mesh packets received through WireGuard continue normally. */
	if (outer->nexthdr != IPPROTO_IPV6)
		return TC_ACT_OK;
	inner = (void *)(outer + 1);
	if ((void *)(inner + 1) > end)
		return TC_ACT_SHOT;
	destination_vm = inner->daddr;
	if (!is_virtual_machine_address(&inner->saddr) ||
		!is_virtual_machine_address(&destination_vm) ||
		!tenants_can_communicate(&inner->saddr, &destination_vm))
		return TC_ACT_SHOT;
	if (is_local_virtual_machine(&destination_vm)) {
		/*
			 * Remove the Atlas WG Mesh outer header. The host route selects the VM interface.
		 *
		 * BPF_F_ADJ_ROOM_FIXED_GSO keeps the segment size unchanged. GRO can
		 * join tunnel packets on wg0. Without this flag, the kernel adds 40
			 * bytes to each segment. The 1380-byte VM interface then rejects the 1420-byte
		 * segment.
		 */
		emit_packet_debug_event(DEBUG_WIREGUARD, DEBUG_ACCEPT,
					&inner->saddr, &inner->daddr);
		if (bpf_skb_adjust_room(packet, -(int)sizeof(struct ipv6hdr),
					BPF_ADJ_ROOM_MAC,
					BPF_F_ADJ_ROOM_NO_CSUM_RESET |
					BPF_F_ADJ_ROOM_FIXED_GSO))
			return TC_ACT_SHOT;
		return TC_ACT_OK;
	}

	return send_not_here_message(packet, local_config, &destination_vm,
				     &sending_host);
}

#endif /* ATLAS_WIREGUARD_HOOK_H */
