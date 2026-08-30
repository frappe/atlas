/* SPDX-License-Identifier: AGPL-3.0 */
/* Build Atlas WG Mesh discovery and recovery packets. */
#ifndef ATLAS_CONTROL_H
#define ATLAS_CONTROL_H

#include "address.h"
#include "debug.h"
#include "state.h"
#include "packet.h"

/* Replace packet with a complete IPv4 and UDP Atlas WG Mesh discovery message. */
static __always_inline int send_discovery_message(
	struct __sk_buff *packet, struct config *local_config,
	const __u8 *recipient_mac, __be32 recipient_ipv4,
	__u8 message_operation, const struct in6_addr *virtual_machine,
	const struct in6_addr *virtual_machine_host, __u32 egress_ifindex)
{
	struct atlas_msg msg = {};
	struct ethhdr eth = {};
	struct udphdr udp = {};
	struct iphdr ip = {};

	__builtin_memcpy(eth.h_dest, recipient_mac, ETH_ALEN);
	__builtin_memcpy(eth.h_source, local_config->uplink_mac, ETH_ALEN);
	eth.h_proto = bpf_htons(ETH_P_IP);

	ip.version = 4;
	ip.ihl = 5;
	ip.tot_len = bpf_htons((__u16)(DISCOVERY_PACKET_LENGTH - ETH_HLEN));
	ip.ttl = recipient_ipv4 == bpf_htonl(ATLAS_MCAST4) ? ATLAS_MULTICAST_TTL : ATLAS_UNICAST_TTL;
	ip.protocol = IPPROTO_UDP;
	ip.saddr = local_config->underlay_ip4;
	ip.daddr = recipient_ipv4;
	ip.check = calculate_ipv4_checksum(&ip);

	udp.source = bpf_htons(ATLAS_PORT);
	udp.dest = bpf_htons(ATLAS_PORT);
	udp.len = bpf_htons((__u16)(DISCOVERY_PACKET_LENGTH - DISCOVERY_UDP_HEADER_OFFSET));

	msg.ver = ATLAS_VER;
	msg.op = message_operation;
	msg.vm = *virtual_machine;

	if (virtual_machine_host)
	{
		msg.host = *virtual_machine_host;
	}

	/* The original packet is intentionally consumed and replaced by this message. */
	emit_protocol_debug_event(DEBUG_UPLINK, DEBUG_SEND, message_operation,
							  virtual_machine, virtual_machine_host);

	if (set_packet_length(packet, DISCOVERY_PACKET_LENGTH))
		return TC_ACT_SHOT;

	if (bpf_skb_store_bytes(packet, 0, &eth, sizeof(eth), 0) ||
		bpf_skb_store_bytes(packet, DISCOVERY_IPV4_HEADER_OFFSET, &ip, sizeof(ip), 0) ||
		bpf_skb_store_bytes(packet, DISCOVERY_UDP_HEADER_OFFSET, &udp, sizeof(udp), 0) ||
		bpf_skb_store_bytes(packet, DISCOVERY_MESSAGE_OFFSET, &msg, sizeof(msg), 0))
		return TC_ACT_SHOT;

	return bpf_redirect(egress_ifindex, 0);
}

/* Send a host discovery request for a virtual machine. */
static __always_inline int ask_for_virtual_machine_host(
	struct __sk_buff *packet, struct config *local_config,
	const struct in6_addr *virtual_machine)
{
	__u8 multicast_mac[ETH_ALEN] = {0x01, 0x00, 0x5e, 0x01, 0x01, 0x01};

	/* Resize before changing the protocol to account for the smaller IPv4 header. */
	if (bpf_skb_change_tail(packet, DISCOVERY_PACKET_LENGTH + sizeof(struct ipv6hdr) - sizeof(struct iphdr), 0))
		return TC_ACT_SHOT;

	/* The guest packet was IPv6; the discovery frame is IPv4. */
	if (bpf_skb_change_proto(packet, bpf_htons(ETH_P_IP), 0))
		return TC_ACT_SHOT;

	return send_discovery_message(packet, local_config, multicast_mac,
								  bpf_htonl(ATLAS_MCAST4), MESSAGE_OPERATION_WHO_HAS,
								  virtual_machine, NULL, local_config->discovery_ifindex);
}

/* Tell the requesting host that its VM location is stale. */
static __always_inline int send_not_here_message(
	struct __sk_buff *packet, struct config *local_config,
	const struct in6_addr *virtual_machine,
	const struct in6_addr *requesting_host)
{
	struct atlas_msg msg = {};
	struct ipv6hdr ip6 = {};

	ip6.version = 6;
	ip6.payload_len = bpf_htons((__u16)sizeof(msg));
	ip6.nexthdr = ATLAS_CONTROL_NEXT_HEADER;
	ip6.hop_limit = 64;
	ip6.saddr = local_config->wg_ip6;
	ip6.daddr = *requesting_host;

	msg.ver = ATLAS_VER;
	msg.op = MESSAGE_OPERATION_NOT_HERE;
	msg.vm = *virtual_machine;
	msg.host = local_config->wg_ip6;

	emit_protocol_debug_event(DEBUG_WIREGUARD, DEBUG_SEND, MESSAGE_OPERATION_NOT_HERE, virtual_machine, &local_config->wg_ip6);

	if (set_packet_length(packet, WIREGUARD_CONTROL_PACKET_LENGTH))
		return TC_ACT_SHOT;

	if (bpf_skb_store_bytes(packet, 0, &ip6, sizeof(ip6), 0) ||
		bpf_skb_store_bytes(packet, WIREGUARD_CONTROL_MESSAGE_OFFSET, &msg, sizeof(msg), 0))
		return TC_ACT_SHOT;

	/* packet->ifindex is wg0 here; WireGuard encrypts the reply to the sender. */
	return bpf_redirect(packet->ifindex, 0);
}

#endif /* ATLAS_CONTROL_H */
