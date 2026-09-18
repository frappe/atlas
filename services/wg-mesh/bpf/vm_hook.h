/* SPDX-License-Identifier: AGPL-3.0 */
/* TC ingress hook for every VM interface. */
#ifndef ATLAS_VM_HOOK_H
#define ATLAS_VM_HOOK_H

#include "debug.h"
#include "state.h"

enum vm_debug_operation
{
	DEBUG_VM_UNDERLAY = 1,
	DEBUG_VM_NOT_VIRTUAL,
	DEBUG_VM_SOURCE_NOT_OWNED,
	DEBUG_VM_TENANT_DENIED,
	DEBUG_VM_LOCAL_DESTINATION,
	DEBUG_VM_REMOTE_UNKNOWN,
	DEBUG_VM_NO_CONFIG,
	DEBUG_VM_ENCAP_FAILED,
	DEBUG_VM_ENCAP_SUCCEEDED,
};

static __always_inline void emit_vm_debug_event(__u8 verdict, __u8 operation, const struct in6_addr *source, const struct in6_addr *destination)
{
	struct debug_event *event;

	if (!is_debug_enabled()) return;

	record_debug_stats(verdict, DEBUG_NO_DIRECTION);

	event = bpf_ringbuf_reserve(&debug_events, sizeof(*event), 0);

	if (!event)
	{
		record_debug_event_loss();
		return;
	}

	__builtin_memset(event, 0, sizeof(*event));

	event->timestamp = bpf_ktime_get_ns();
	event->hook = DEBUG_VM;
	event->verdict = verdict;
	event->operation = operation;
	event->source = *source;
	event->destination = *destination;

	__builtin_memcpy(event->tenant, &source->s6_addr[4], 4);

	bpf_ringbuf_submit(event, 0);
}

/* Build the IPv6 solicited-node multicast address ff02::1:ffXX:XXXX,
 * where XX:XXXX are the low 24 bits of the target address.
 */
static __always_inline void make_solicited_node_address(const struct in6_addr *target, struct in6_addr *destination)
{
	__builtin_memset(destination, 0, sizeof(*destination));

	destination->s6_addr[0] = 0xff;
	destination->s6_addr[1] = 0x02;

	destination->s6_addr[11] = 0x01;
	destination->s6_addr[12] = 0xff;

	destination->s6_addr[13] = target->s6_addr[13];
	destination->s6_addr[14] = target->s6_addr[14];
	destination->s6_addr[15] = target->s6_addr[15];
}

/* Fold a checksum returned by bpf_csum_diff(). */
static __always_inline __u16 fold_checksum(__s64 checksum)
{
	checksum = (checksum & 0xffffffff) + (checksum >> 32);

	checksum = (checksum & 0xffff) + (checksum >> 16);

	checksum = (checksum & 0xffff) + (checksum >> 16);

	return (__u16)~checksum;
}

/* Construct and checksum an ICMPv6 Neighbor Solicitation. The checksum
 * covers the IPv6 pseudo-header, the ICMPv6 NS header, the target
 * address, and the Source Link-Layer Address option.
 */
static __always_inline int checksum_neighbor_solicitation(const struct ipv6hdr *ip6, void *message, __u32 message_length, __u16 *checksum)
{
	struct
	{
		struct in6_addr source;
		struct in6_addr destination;
		__be32 length;
		__u8 zero[3];
		__u8 next_header;
	} pseudo = {};

	__s64 sum;

	pseudo.source = ip6->saddr;
	pseudo.destination = ip6->daddr;
	pseudo.length = bpf_htonl(message_length);
	pseudo.next_header = IPPROTO_ICMPV6;

	sum = bpf_csum_diff(0, 0, (__be32 *)&pseudo, sizeof(pseudo), 0);

	if (sum < 0) return -1;

	sum = bpf_csum_diff(0, 0, (__be32 *)message, message_length, sum);

	if (sum < 0) return -1;

	*checksum = fold_checksum(sum);

	return 0;
}

/* Replace the current VM packet with a standard multicast ICMPv6 Neighbor
 * Solicitation: Ethernet, IPv6, ICMPv6 NS, Source Link-Layer Address
 * option. The caller redirects the result to discovery_ifindex.
 */
static __always_inline int send_neighbor_solicitation(struct __sk_buff *packet, struct config *local_config, const struct in6_addr *source, const struct in6_addr *target)
{
	struct ethhdr eth = {};
	struct ipv6hdr ip6 = {};

	struct in6_addr solicited_node;

	struct
	{
		struct icmp6hdr icmp6;

		struct in6_addr target;

		struct
		{
			__u8 type;
			__u8 length;
			__u8 address[ETH_ALEN];
		} slla;
	} __attribute__((packed)) message = {};

	__u16 checksum;

	const __u32 message_length = sizeof(message);

	const __u32 packet_length = sizeof(eth) + sizeof(ip6) + sizeof(message);

		/* ff02::1:ffXX:XXXX */
	make_solicited_node_address(target, &solicited_node);

		/* IPv6 multicast maps to 33:33:ff:XX:XX:XX. */
	eth.h_dest[0] = 0x33;
	eth.h_dest[1] = 0x33;
	eth.h_dest[2] = solicited_node.s6_addr[12];
	eth.h_dest[3] = solicited_node.s6_addr[13];
	eth.h_dest[4] = solicited_node.s6_addr[14];
	eth.h_dest[5] = solicited_node.s6_addr[15];

		/* This packet is transmitted on the discovery interface, not the VM
	 * interface.
	 */
	__builtin_memcpy(eth.h_source, local_config->discovery_mac, ETH_ALEN);

	eth.h_proto = bpf_htons(ETH_P_IPV6);

		/* IPv6 header. */
	ip6.version = 6;
	ip6.payload_len = bpf_htons(message_length);
	ip6.nexthdr = IPPROTO_ICMPV6;
	ip6.hop_limit = 255;

		/* The source is the VM that triggered discovery. */
	ip6.saddr = *source;
	ip6.daddr = solicited_node;

	message.icmp6.icmp6_type = NDISC_NEIGHBOUR_SOLICITATION;

	message.icmp6.icmp6_code = 0;
	message.icmp6.icmp6_cksum = 0;

		/* The reserved field in struct icmp6hdr is already zero because message
	 * was initialized with {}.
	 */

	message.target = *target;

		/* Source Link-Layer Address option: type 1, length 1 (one 8-byte unit). */
	message.slla.type = 1;
	message.slla.length = 1;

	__builtin_memcpy(message.slla.address, local_config->discovery_mac, ETH_ALEN);

	if (checksum_neighbor_solicitation(&ip6, &message, message_length, &checksum)) return -1;

	message.icmp6.icmp6_cksum = checksum;

		/* Replace the original VM packet with the NS. bpf_skb_change_tail() may
	 * invalidate packet pointers, so everything above is built from local
	 * stack objects.
	 */
	if (bpf_skb_change_tail(packet, packet_length, 0)) return -1;

	if (bpf_skb_store_bytes(packet, 0, &eth, sizeof(eth), BPF_F_INVALIDATE_HASH)) return -1;

		/* IPv6 header. */
	if (bpf_skb_store_bytes(packet, ETH_HLEN, &ip6, sizeof(ip6), BPF_F_INVALIDATE_HASH)) return -1;

	if (bpf_skb_store_bytes(packet, ETH_HLEN + sizeof(ip6), &message, sizeof(message), BPF_F_INVALIDATE_HASH)) return -1;

	return 0;
}

static __always_inline int add_tunnel_header(struct __sk_buff *packet, struct config *local_config, const struct in6_addr *remote_host, __u16 inner_packet_length)
{
	struct ipv6hdr outer = {};
	long ret;

	outer.version = 6;
	outer.payload_len = bpf_htons(inner_packet_length);
	outer.nexthdr = IPPROTO_IPV6;
	outer.hop_limit = 64;
	outer.saddr = local_config->wg_ip6;
	outer.daddr = *remote_host;

	ret = bpf_skb_adjust_room(packet, sizeof(outer), BPF_ADJ_ROOM_MAC, BPF_F_ADJ_ROOM_FIXED_GSO | BPF_F_ADJ_ROOM_ENCAP_L3_IPV6 | BPF_F_ADJ_ROOM_NO_CSUM_RESET);

	if (ret)
	{
		emit_vm_debug_event(DEBUG_DROP, DEBUG_VM_ENCAP_FAILED, &local_config->wg_ip6, remote_host);

		return TC_ACT_SHOT;
	}

	ret = bpf_skb_store_bytes(packet, ETH_HLEN, &outer, sizeof(outer), BPF_F_INVALIDATE_HASH);

	if (ret)
	{
		emit_vm_debug_event(DEBUG_DROP, DEBUG_VM_ENCAP_FAILED, &local_config->wg_ip6, remote_host);

		return TC_ACT_SHOT;
	}

	emit_vm_debug_event(DEBUG_REDIRECT, DEBUG_VM_ENCAP_SUCCEEDED, &outer.saddr, &outer.daddr);

	return TC_ACT_OK;
}

/* Attached to TC ingress on every VM interface. Remote VM traffic is
 * encapsulated as Ethernet, IPv6 (outer WG host to remote WG host), IPv6
 * (inner VM to VM), and sent through Linux routing to wg0.
 */
SEC("tc")
int handle_vm_packet(struct __sk_buff *packet)
{
	void *data = (void *)(long)packet->data;
	void *end = (void *)(long)packet->data_end;

	struct ethhdr *eth = data;
	struct ipv6hdr *ip6;

	struct in6_addr src;
	struct in6_addr dst;

	struct config *local_config;
	struct in6_addr *remote_host;

	__u16 inner_packet_length;

	if ((void *)(eth + 1) > end || eth->h_proto != bpf_htons(ETH_P_IPV6)) return TC_ACT_OK;

	ip6 = (void *)(eth + 1);

	if ((void *)(ip6 + 1) > end) return TC_ACT_OK;

	src = ip6->saddr;
	dst = ip6->daddr;

	inner_packet_length = (__u16)(bpf_ntohs(ip6->payload_len) + sizeof(*ip6));

	/*
	 * Guests must never inject packets directly to an underlay address.
	 */
	if (is_underlay_address(&dst))
	{
		emit_vm_debug_event(DEBUG_DROP, DEBUG_VM_UNDERLAY, &src, &dst);

		return TC_ACT_SHOT;
	}

	/*
	 * Only fdaa::/16 traffic belongs to the mesh.
	 */
	if (!is_virtual_machine_address(&dst))
	{
		emit_vm_debug_event(DEBUG_ACCEPT, DEBUG_VM_NOT_VIRTUAL, &src, &dst);

		return TC_ACT_OK;
	}

	/*
	 * Only a registered local VM may inject mesh traffic.
	 */
	if (!owns_source_address(&src, packet->ifindex))
	{
		emit_vm_debug_event(DEBUG_DROP, DEBUG_VM_SOURCE_NOT_OWNED, &src, &dst);

		return TC_ACT_SHOT;
	}

	/*
	 * Enforce tenant isolation.
	 */
	if (!tenants_can_communicate(&src, &dst))
	{
		emit_vm_debug_event(DEBUG_DROP, DEBUG_VM_TENANT_DENIED, &src, &dst);

		return TC_ACT_SHOT;
	}

	/*
	 * Same-host VM traffic stays on the normal Linux path.
	 */
	if (is_local_virtual_machine(&dst))
	{
		emit_vm_debug_event(DEBUG_ACCEPT, DEBUG_VM_LOCAL_DESTINATION, &src, &dst);

		return TC_ACT_OK;
	}

	/*
	 * Find the Atlas host owning the remote VM.
	 */
	remote_host = get_remote_location(&dst);

	if (!remote_host)
	{
				/* The remote VM is unknown locally: trigger multicast NDP discovery. */
		local_config = get_config();

		if (!local_config)
		{
			emit_vm_debug_event(DEBUG_DROP, DEBUG_VM_NO_CONFIG, &src, &dst);

			return TC_ACT_SHOT;
		}

				/* Replace this VM packet with a multicast Neighbor Solicitation. */
		if (send_neighbor_solicitation(packet, local_config, &src, &dst))
		{
			emit_vm_debug_event(DEBUG_DROP, DEBUG_VM_ENCAP_FAILED, &src, &dst);

			return TC_ACT_SHOT;
		}

		emit_vm_debug_event(DEBUG_REDIRECT, DEBUG_VM_REMOTE_UNKNOWN, &src, &dst);

				/* Send the newly constructed NS through the shared discovery VLAN. */
		return bpf_redirect(local_config->discovery_ifindex, 0);
	}

	local_config = get_config();

	if (!local_config)
	{
		emit_vm_debug_event(DEBUG_DROP, DEBUG_VM_NO_CONFIG, &src, &dst);

		return TC_ACT_SHOT;
	}

		/* Remote location is known: encapsulate the VM packet. */
	return add_tunnel_header(packet, local_config, remote_host, inner_packet_length);
}

#endif /* ATLAS_VM_HOOK_H */
