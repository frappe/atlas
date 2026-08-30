/* SPDX-License-Identifier: AGPL-3.0 */
/* TC ingress hook for the physical uplink: Atlas WG Mesh discovery only. */
#ifndef ATLAS_UPLINK_HOOK_H
#define ATLAS_UPLINK_HOOK_H

#include "control.h"

/*
 * Attached to TC ingress on the physical uplink.
 *
 * Only Atlas WG Mesh IPv4 and UDP discovery frames reach this hook. The VM
 * host replies to WHO_HAS with FOUND. FOUND records a remote VM location.
 * Atlas WG Mesh frames are always consumed; all unrelated network traffic continues
 * through the host unchanged.
 */
SEC("tc")
int handle_uplink_packet(struct __sk_buff *packet)
{
	void *data = (void *)(long)packet->data;
	void *end = (void *)(long)packet->data_end;
	struct ethhdr *eth = data;
	struct iphdr *ip;
	struct udphdr *udp;
	struct atlas_msg msg;
	struct config *local_config;
	__u8 sender_mac[ETH_ALEN];
	__be32 sender_ipv4;

	if ((void *)(eth + 1) > end || eth->h_proto != bpf_htons(ETH_P_IP))
		return TC_ACT_OK;
	ip = (void *)(eth + 1);
	if ((void *)(ip + 1) > end || ip->protocol != IPPROTO_UDP || ip->ihl != 5)
		return TC_ACT_OK;
	udp = (void *)(ip + 1);
	if ((void *)(udp + 1) > end || udp->dest != bpf_htons(ATLAS_PORT))
		return TC_ACT_OK;

	local_config = get_config();
	if (!local_config)
		return TC_ACT_SHOT;
	__builtin_memcpy(sender_mac, eth->h_source, ETH_ALEN);
	sender_ipv4 = ip->saddr;
	if (sender_ipv4 == local_config->underlay_ip4)
		return TC_ACT_SHOT; /* Our own multicast frame was reflected. */
	if (bpf_skb_load_bytes(packet, DISCOVERY_MESSAGE_OFFSET, &msg,
			       sizeof(msg)) ||
	    msg.ver != ATLAS_VER || !is_virtual_machine_address(&msg.vm))
		return TC_ACT_SHOT;
	/* Discovery is link-local; the Ethernet/IP sender is the reply destination. */
	emit_protocol_debug_event(DEBUG_UPLINK, DEBUG_RECEIVE, msg.op,
				  &msg.vm, &msg.host);

	if (msg.op == MESSAGE_OPERATION_WHO_HAS) {
		if (!is_local_virtual_machine(&msg.vm))
			return TC_ACT_SHOT;
		/* Unicast FOUND to the requester instead of replying to multicast. */
		return send_discovery_message(packet, local_config, sender_mac, sender_ipv4,
				      MESSAGE_OPERATION_FOUND, &msg.vm, &local_config->wg_ip6,
				      packet->ifindex);
	}
	/*
	 * FOUND answers a WHO_HAS that this host sent, so it creates the
	 * record. NOW_HERE is unsolicited, so it only corrects a record that
	 * this host already holds. A VM never enters a cache that did not ask
	 * for it.
	 */
	if (msg.op == MESSAGE_OPERATION_FOUND ||
	    msg.op == MESSAGE_OPERATION_NOW_HERE) {
		__u64 mode = msg.op == MESSAGE_OPERATION_FOUND ?
			BPF_ANY : BPF_EXIST;

		/* The tunnel destination must be a host WireGuard address. */
		if (!is_underlay_address(&msg.host))
			return TC_ACT_SHOT;

		/* NOW_HERE cannot create unsolicited remote reachability. */
		bpf_map_update_elem(&remote_vms, &msg.vm, &msg.host, mode);
	}

	/* All valid Atlas WG Mesh discovery frames are consumed, never sent to the host. */
	return TC_ACT_SHOT;
}

#endif /* ATLAS_UPLINK_HOOK_H */
