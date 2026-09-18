/* SPDX-License-Identifier: AGPL-3.0 */
/* TC hooks that carry NDP across a routed IPv4 underlay. */
#ifndef ATLAS_NDP_UNICAST_HOOK_H
#define ATLAS_NDP_UNICAST_HOOK_H

#include <linux/ip.h>

#include "debug.h"
#include "state.h"

/* Byte offsets inside the headers the hooks rewrite. */
#define ATLAS_UNICAST_PAYLOAD_LENGTH_OFFSET 4
#define ATLAS_UNICAST_CHECKSUM_OFFSET 2
#define ATLAS_UNICAST_ETHERTYPE_OFFSET 12
#define ATLAS_UNICAST_PEER_ADDRESS_OFFSET 2
#define ATLAS_UNICAST_HOST_ADDRESS_OFFSET 4
#define ATLAS_UNICAST_IPV4_DESTINATION_OFFSET 16
#define ATLAS_UNICAST_IPV4_CHECKSUM_OFFSET 10

/* Limit the option walks, so the verifier can bound the loops. */
#define ATLAS_UNICAST_NS_OPTION_WALK_LIMIT 8
#define ATLAS_UNICAST_NA_OPTION_WALK_LIMIT 16

/* Solicitation options that are preserved and transported. The kernel adds
 * at most a source link-layer address option to a solicitation. */
#define ATLAS_UNICAST_NS_OPTIONS_LIMIT 32

/* 16-bit words in the preserved options of one solicitation. */
#define ATLAS_UNICAST_NS_OPTION_WORD_LIMIT 16

/* Address family for IPv4 in bpf_fib_lookup. */
#define ATLAS_UNICAST_AF_INET 2

enum unicast_debug_operation
{
	UNICAST_OPERATION_EGRESS_KNOWN_PEER = 1,
	UNICAST_OPERATION_EGRESS_FAN_OUT,
	UNICAST_OPERATION_EGRESS_SEND_FAILED,
	UNICAST_OPERATION_EGRESS_ADVERTISEMENT,
	UNICAST_OPERATION_EGRESS_NO_REQUESTER,
	UNICAST_OPERATION_EGRESS_APPEND_FAILED,
	UNICAST_OPERATION_EGRESS_WRAP_FAILED,
	UNICAST_OPERATION_INGRESS_ACCEPTED,
	UNICAST_OPERATION_INGRESS_PEER_REJECTED,
	UNICAST_OPERATION_INGRESS_REQUESTER_STORED,
	UNICAST_OPERATION_INGRESS_LOCATION_LEARNED,
	UNICAST_OPERATION_INGRESS_REMOTE_LEARNED,
	UNICAST_OPERATION_INGRESS_KFUNC_FAILED,
	UNICAST_OPERATION_INGRESS_NO_ATLAS_OPTION,
	UNICAST_OPERATION_EGRESS_ATLAS_APPEND_FAILED,
	UNICAST_OPERATION_EGRESS_CSUM_FAILED,
	UNICAST_OPERATION_EGRESS_FIB_FAILED,
	UNICAST_OPERATION_EGRESS_DST_MAC_FAILED,
	UNICAST_OPERATION_EGRESS_SRC_MAC_FAILED,
	UNICAST_OPERATION_EGRESS_REDIRECT_FAILED,
};

/* IPv6 pseudo-header used for the ICMPv6 checksum of a changed message. */
struct atlas_unicast_pseudo_header
{
	struct in6_addr saddr;
	struct in6_addr daddr;
	__be32 length;
	__u8 zero[3];
	__u8 nexthdr;
};

/* Atlas neighbour kfunc, provided by the Atlas kernel module. The unicast
 * hook registers learned VM neighbours itself, because the multicast NDP
 * hook is not attached in a unicast environment. addr_hi/addr_lo carry the
 * first/last 8 bytes of the IPv6 address, and the MAC is packed into the
 * low 6 bytes of mac.
 */
extern int atlas_register_neigh(__u32 ifindex, __u64 addr_hi, __u64 addr_lo, __u64 mac) __ksym;

/* Standard NDP Target Link-Layer Address option. Total wire size: 8 bytes. */
#define ATLAS_UNICAST_TLLAO_TYPE 2
#define ATLAS_UNICAST_TLLAO_LENGTH 1

struct atlas_unicast_tllao
{
	__u8 type;
	__u8 length;
	__u8 mac[ETH_ALEN];
};

/* Everything appended to a Neighbor Advertisement: the standard TLLAO and
 * the Atlas option with the owning host WireGuard address. The wire format
 * matches the multicast NDP hook, so a receiving host learns the same
 * advertisement in both modes.
 */
struct atlas_unicast_advertisement_append
{
	struct atlas_unicast_tllao tllao;
	struct atlas_ndp_option atlas;
};

/* Fold a one's-complement checksum down to 16 bits. */
static __always_inline __u16 atlas_unicast_csum_fold(__u32 sum)
{
	sum = (sum & 0xffff) + (sum >> 16);
	sum = (sum & 0xffff) + (sum >> 16);

	return (__u16)~sum;
}

/* Check if the given IPv4 address belongs to a configured peer. Peers occupy
 * the low indexes of the peer_list map densely; the first zero value ends
 * the list. The map key is a separate stack variable, because its address
 * is taken. The induction variable then stays in a register, so the
 * verifier can bound the loop.
 */
static __always_inline int atlas_unicast_is_known_peer(__be32 peer)
{
	__u32 index;

	for (index = 0; index < ATLAS_UNICAST_PEER_LIMIT; index++) {
		__u32 key = index;

		__be32 *candidate = bpf_map_lookup_elem(&peer_list, &key);

		if (!candidate || *candidate == 0) return 0;

		if (*candidate == peer) return 1;
	}

	return 0;
}

/* Find the Atlas requester option in a solicitation. The option carries the
 * IPv4 address that must receive the answer. Packet pointers stay inside
 * the bounded loop, and the value is copied to stack memory through
 * bpf_skb_load_bytes.
 */
static __always_inline int atlas_unicast_find_requester(struct __sk_buff *packet, const struct ipv6hdr *ip6, __u32 packet_ipv6_offset, __be32 *peer_address)
{
	__u16 payload_length;
	__u32 options_length;
	__u32 options_offset;
	__u32 offset;
	__u8 option_header[2];
	__u8 type;
	__u8 length;
	__u32 option_size;
	__u32 option_address_offset;
	int step;

		/* The IPv6 payload length is the authoritative boundary for the
	 * ICMPv6/NDP message, not the outer data_end: this is an IPv6 packet
	 * carried inside an outer IPv4 packet.
	 */
	payload_length = bpf_ntohs(ip6->payload_len);

	if (payload_length < sizeof(struct ndp_message)) return 0;

	options_length = (__u32)payload_length - sizeof(struct ndp_message);

		/* A solicitation only needs a small option area: a source link-layer
	 * option followed by our 16-byte Atlas option. Keep the bound
	 * verifier-friendly and reject malformed packets.
	 */
	if (options_length > ATLAS_UNICAST_NS_OPTIONS_LIMIT) return 0;

	options_offset = packet_ipv6_offset + sizeof(struct ipv6hdr) + sizeof(struct ndp_message);

	offset = 0;

	for (step = 0; step < ATLAS_UNICAST_NS_OPTION_WALK_LIMIT; step++) {
		if (offset >= options_length) break;

		if (options_length - offset < 2) return 0;

		if (bpf_skb_load_bytes(packet, options_offset + offset, option_header, sizeof(option_header))) return 0;

		type = option_header[0];
		length = option_header[1];

		if (length == 0) return 0;

		option_size = (__u32)length * 8;

		if (option_size > options_length - offset) return 0;

		if (type == ATLAS_NS_OPTION_TYPE) {
			if (length != ATLAS_NS_OPTION_UNITS + 1) return 0;

			option_address_offset = options_offset + offset + ATLAS_UNICAST_PEER_ADDRESS_OFFSET;

			if (option_size < ATLAS_UNICAST_PEER_ADDRESS_OFFSET + sizeof(*peer_address)) return 0;

			if (bpf_skb_load_bytes(packet, option_address_offset, peer_address, sizeof(*peer_address))) return 0;

			return 1;
		}

		offset += option_size;
	}

	return 0;
}

/* Find the Atlas option in an advertisement and copy the host address. Only
 * an advertisement that carries a valid Atlas option for an underlay host
 * records a peer location.
 */
static __always_inline int atlas_unicast_find_atlas_host(struct __sk_buff *packet, const struct ipv6hdr *ip6, __u32 packet_ipv6_offset, struct in6_addr *host)
{
	__u16 payload_length;
	__u32 options_length;
	__u32 options_offset;
	__u32 offset;
	__u8 option_header[2];
	__u8 type;
	__u8 length;
	__u32 option_size;
	__u32 option_address_offset;
	int step;

		/* Use the inner IPv6 payload length as the NDP option boundary. */
	payload_length = bpf_ntohs(ip6->payload_len);

	if (payload_length < sizeof(struct ndp_message)) return 0;

	options_length = (__u32)payload_length - sizeof(struct ndp_message);

	if (options_length > 128) return 0;

	options_offset = packet_ipv6_offset + sizeof(struct ipv6hdr) + sizeof(struct ndp_message);

	offset = 0;

	for (step = 0; step < ATLAS_UNICAST_NA_OPTION_WALK_LIMIT; step++) {
		if (offset >= options_length) break;

		if (options_length - offset < 2) return 0;

		if (bpf_skb_load_bytes(packet, options_offset + offset, option_header, sizeof(option_header))) return 0;

		type = option_header[0];
		length = option_header[1];

		if (length == 0) return 0;

		option_size = (__u32)length * 8;

		if (option_size > options_length - offset) return 0;

		if (type == ATLAS_NDP_OPTION_TYPE) {
			if (length != ATLAS_NDP_OPTION_LENGTH + 1) return 0;

			option_address_offset = options_offset + offset + ATLAS_UNICAST_HOST_ADDRESS_OFFSET;

			if (option_size < ATLAS_UNICAST_HOST_ADDRESS_OFFSET + sizeof(*host)) return 0;

			if (bpf_skb_load_bytes(packet, option_address_offset, host, sizeof(*host))) return 0;

			return 1;
		}

		offset += option_size;
	}

	return 0;
}

/* Append the Atlas requester option to a solicitation: it tells the
 * answering peer which IPv4 address must receive the answer. Existing
 * options, such as the source link-layer address, are preserved, so the
 * answering kernel can reply without a new neighbour lookup. Returns 1 on
 * success and stores the new payload length.
 *
 * The checksum is calculated from scratch over the fixed message, the
 * preserved options, the new option, and the IPv6 pseudo-header. All
 * packet-derived values are copied to stack memory before any packet
 * change, because bpf_skb_change_tail() invalidates packet pointers.
 */
static __always_inline int atlas_unicast_append_requester_option(struct __sk_buff *packet, const struct ipv6hdr *ip6, struct ndp_message *message, void *end, __be32 peer_address, __u16 *new_payload_length)
{
	struct atlas_unicast_pseudo_header pseudo = {};
	struct atlas_ns_option option = {};
	struct ndp_message fixed_message = {};

	struct in6_addr source;
	struct in6_addr destination;

	__s64 checksum_sum = 0;

	__u16 checksum;
	__u16 wire_payload_length;

	__u16 old_payload_length;
	__u32 option_bytes;
	__u32 options_offset;

	__u32 step;
	__be32 option_word;

	old_payload_length = bpf_ntohs(ip6->payload_len);

	if (old_payload_length < sizeof(struct ndp_message) || old_payload_length > sizeof(struct ndp_message) + ATLAS_UNICAST_NS_OPTIONS_LIMIT) return 0;

	option_bytes = old_payload_length - sizeof(struct ndp_message);

	/* NDP options use units of 8 bytes. */
	if (option_bytes & 7) return 0;

	source = ip6->saddr;
	destination = ip6->daddr;

	if (bpf_skb_load_bytes(packet, ETH_HLEN + sizeof(struct ipv6hdr), &fixed_message, sizeof(fixed_message))) return 0;

	fixed_message.icmp.icmp6_cksum = 0;

	option.type = ATLAS_NS_OPTION_TYPE;
	option.length = ATLAS_NS_OPTION_UNITS + 1;
	__builtin_memcpy(option.peer_ipv4, &peer_address, sizeof(option.peer_ipv4));

	*new_payload_length =
		old_payload_length + sizeof(option);
	wire_payload_length = bpf_htons(*new_payload_length);

	options_offset = ETH_HLEN + sizeof(struct ipv6hdr) + sizeof(struct ndp_message);

		/* bpf_csum_diff() returns a Linux __wsum/partial checksum. Its seed is
	 * designed to be the result of a previous bpf_csum_diff() call, so
	 * components must be cascaded, not added as ordinary integers. The order
	 * here is the actual ICMPv6 checksum input: pseudo-header, fixed message,
	 * existing NDP options, then the new Atlas requester option.
	 */
	pseudo.saddr = source;
	pseudo.daddr = destination;
	pseudo.length = bpf_htonl(*new_payload_length);
	pseudo.nexthdr = IPPROTO_ICMPV6;

	checksum_sum = bpf_csum_diff(NULL, 0, (__be32 *)&pseudo, sizeof(pseudo), 0);
	if (checksum_sum < 0) return 0;

	checksum_sum = bpf_csum_diff(NULL, 0, (__be32 *)&fixed_message, sizeof(fixed_message), (__wsum)checksum_sum);
	if (checksum_sum < 0) return 0;

		/* Preserve every byte of the original NDP options. The area is bounded to
	 * 32 bytes and is a multiple of 8, so eight bounded 4-byte helper calls
	 * are sufficient and verifier-friendly.
	 */
	for (step = 0; step < ATLAS_UNICAST_NS_OPTION_WORD_LIMIT / 2; step++) {
		if (step * 4 >= option_bytes) break;

		if (bpf_skb_load_bytes(packet, options_offset + step * 4, &option_word, sizeof(option_word))) return 0;

		checksum_sum = bpf_csum_diff(NULL, 0, &option_word, sizeof(option_word), (__wsum)checksum_sum);
		if (checksum_sum < 0) return 0;
	}

	checksum_sum = bpf_csum_diff(NULL, 0, (__be32 *)&option, sizeof(option), (__wsum)checksum_sum);
	if (checksum_sum < 0) return 0;

	options_offset = ETH_HLEN + sizeof(struct ipv6hdr) + sizeof(struct ndp_message);

	if (bpf_skb_change_tail(packet, packet->len + sizeof(option), 0)) return 0;

	if (bpf_skb_store_bytes(packet, options_offset + option_bytes, &option, sizeof(option), 0)) return 0;

	if (bpf_skb_store_bytes(packet, ETH_HLEN + ATLAS_UNICAST_PAYLOAD_LENGTH_OFFSET, &wire_payload_length, sizeof(wire_payload_length), 0)) return 0;

	checksum = atlas_unicast_csum_fold((__u32)checksum_sum);

	if (bpf_skb_store_bytes(packet, ETH_HLEN + sizeof(struct ipv6hdr) + ATLAS_UNICAST_CHECKSUM_OFFSET, &checksum, sizeof(checksum), 0)) return 0;

	return 1;
}

/* Append the TLLAO and Atlas options to a Neighbor Advertisement. The TLLAO
 * lets the requesting kernel learn the link-layer address, and the Atlas
 * option carries this host's WireGuard address. Returns 1 on success.
 *
 * The checksum is calculated from scratch over the fixed message, the
 * appended options, and the IPv6 pseudo-header. All packet-derived values
 * are copied to stack memory before any packet change, because
 * bpf_skb_change_tail() invalidates packet pointers.
 */
static __always_inline int atlas_unicast_add_atlas_option(struct __sk_buff *packet, const struct ethhdr *eth, const struct ipv6hdr *ip6, const struct ndp_message *message, const struct config *local_config)
{
	struct atlas_unicast_advertisement_append append = {};

	struct atlas_unicast_pseudo_header pseudo = {};

	struct ndp_message fixed_message = {};

	struct in6_addr source;
	struct in6_addr destination;

	__s64 checksum_sum = 0;

	__u16 checksum;
	__u16 wire_payload_length;

	__u16 old_payload_length;
	__u16 new_payload_length;

	__u32 append_length;
	__u32 append_offset;
	__u32 checksum_offset;
	__u32 existing_options_length;
	__u32 existing_options_offset;
	__u32 checksum_step;
	__be32 existing_option_word;

	append.tllao.type = ATLAS_UNICAST_TLLAO_TYPE;
	append.tllao.length = ATLAS_UNICAST_TLLAO_LENGTH;

	__builtin_memcpy(append.tllao.mac, eth->h_source, ETH_ALEN);

	append.atlas.type = ATLAS_NDP_OPTION_TYPE;
	append.atlas.length = ATLAS_NDP_OPTION_LENGTH + 1;
	append.atlas.host = local_config->wg_ip6;

	append_length = sizeof(append);

	if (append_length != sizeof(struct atlas_unicast_tllao) + sizeof(struct atlas_ndp_option)) return 0;

	source = ip6->saddr;
	destination = ip6->daddr;

	if (bpf_skb_load_bytes(packet, ETH_HLEN + sizeof(struct ipv6hdr), &fixed_message, sizeof(fixed_message))) return 0;

	fixed_message.icmp.icmp6_cksum = 0;

	old_payload_length = bpf_ntohs(ip6->payload_len);

	if ((__u32)old_payload_length + append_length > 0xffff) return 0;

	new_payload_length = old_payload_length + append_length;
	wire_payload_length = bpf_htons(new_payload_length);

	pseudo.saddr = source;
	pseudo.daddr = destination;
	pseudo.length = bpf_htonl(new_payload_length);
	pseudo.nexthdr = IPPROTO_ICMPV6;

	/*
	 * Cascade bpf_csum_diff() through the complete ICMPv6 checksum input:
	 * pseudo-header + fixed NA + existing options + appended options.
	 */
	checksum_sum = bpf_csum_diff(NULL, 0, (__be32 *)&pseudo, sizeof(pseudo), 0);
	if (checksum_sum < 0) return 0;

	checksum_sum = bpf_csum_diff(NULL, 0, (__be32 *)&fixed_message, sizeof(fixed_message), (__wsum)checksum_sum);
	if (checksum_sum < 0) return 0;

	existing_options_length = old_payload_length - sizeof(struct ndp_message);
	existing_options_offset = ETH_HLEN + sizeof(struct ipv6hdr) + sizeof(struct ndp_message);

	if (existing_options_length > 128 || (existing_options_length & 3)) return 0;

	for (checksum_step = 0; checksum_step < 32; checksum_step++) {
		if (checksum_step * 4 >= existing_options_length) break;

		if (bpf_skb_load_bytes(packet, existing_options_offset + checksum_step * 4, &existing_option_word, sizeof(existing_option_word))) return 0;

		checksum_sum = bpf_csum_diff(NULL, 0, &existing_option_word, sizeof(existing_option_word), (__wsum)checksum_sum);
		if (checksum_sum < 0) return 0;
	}

	checksum_sum = bpf_csum_diff(NULL, 0, (__be32 *)&append, sizeof(append), (__wsum)checksum_sum);
	if (checksum_sum < 0) return 0;

	if (bpf_skb_change_tail(packet, packet->len + append_length, 0)) return 0;

	append_offset = ETH_HLEN + sizeof(struct ipv6hdr) + sizeof(struct ndp_message);

	if (bpf_skb_store_bytes(packet, append_offset, &append, append_length, 0)) return 0;

	if (bpf_skb_store_bytes(packet, ETH_HLEN + ATLAS_UNICAST_PAYLOAD_LENGTH_OFFSET, &wire_payload_length, sizeof(wire_payload_length), 0)) return 0;

	checksum = atlas_unicast_csum_fold((__u32)checksum_sum);

	checksum_offset = ETH_HLEN + sizeof(struct ipv6hdr) + ATLAS_UNICAST_CHECKSUM_OFFSET;

	if (bpf_skb_store_bytes(packet, checksum_offset, &checksum, sizeof(checksum), 0)) return 0;

	return 1;
}

/* Wrap a packet in an outer IPv4 header: the header space is added between
 * the Ethernet header and the payload, and the Ethernet type becomes IPv4.
 * The header bytes are stored by the caller per peer.
 */
static __always_inline int atlas_unicast_wrap_ipv4(struct __sk_buff *packet)
{
	__be16 ethernet_type = bpf_htons(ETH_P_IP);

	if (bpf_skb_adjust_room(packet, sizeof(struct iphdr), BPF_ADJ_ROOM_MAC, BPF_F_ADJ_ROOM_FIXED_GSO | BPF_F_ADJ_ROOM_ENCAP_L3_IPV4)) return 0;

	return !bpf_skb_store_bytes(packet, ATLAS_UNICAST_ETHERTYPE_OFFSET, &ethernet_type, sizeof(ethernet_type), BPF_F_INVALIDATE_HASH);
}

/* Store the base outer IPv4 header of a wrapped packet. The destination
 * address stays zero, and every peer copy then stores only its own
 * destination address and the checksum contribution that the destination
 * adds. Returns 1 on success and stores the base checksum sum.
 */
static __always_inline int atlas_unicast_store_base_header(struct __sk_buff *packet, __be32 source_address, __u16 wrapped_length, __s64 *base_sum)
{
	struct iphdr outer = {};

	__s64 header_sum;

	outer.version = 4;
	outer.ihl = 5;
	outer.tot_len = bpf_htons(wrapped_length);
	outer.ttl = 64;
	outer.protocol = IPPROTO_IPV6;
	outer.saddr = source_address;

	header_sum = bpf_csum_diff(NULL, 0, (__be32 *)&outer, sizeof(outer), 0);

	if (header_sum < 0) return 0;

	outer.check = atlas_unicast_csum_fold((__u64)header_sum);

	if (bpf_skb_store_bytes(packet, ETH_HLEN, &outer, sizeof(outer), BPF_F_INVALIDATE_HASH)) return 0;

	*base_sum = header_sum;

	return 1;
}

/* Send one copy of the wrapped packet to a peer: store the peer destination
 * and checksum contribution, resolve the underlay route with
 * bpf_fib_lookup(), and emit the copy with bpf_clone_redirect(). Returns 0
 * on success, otherwise the failing helper/FIB result.
 */
static __always_inline int atlas_unicast_send_to_peer(struct __sk_buff *packet, __be32 *peer_address, __be32 source_address, __u16 wrapped_length, __s64 base_sum, const struct in6_addr *debug_target)
{
	struct bpf_fib_lookup route = {};
	__u16 checksum;
	__s64 peer_sum;
	int fib_result;
	int redirect_result;

		/* Continue the IPv4 checksum from the base header partial sum:
	 * bpf_csum_diff() expects its seed to be the previous partial checksum.
	 */
	peer_sum = bpf_csum_diff(NULL, 0, (__be32 *)peer_address, sizeof(*peer_address), (__wsum)base_sum);

	if (peer_sum < 0) {
		emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_CSUM_FAILED, debug_target, NULL);
		return -1;
	}

	checksum = atlas_unicast_csum_fold((__u32)peer_sum);

	if (bpf_skb_store_bytes(packet, ETH_HLEN + ATLAS_UNICAST_IPV4_DESTINATION_OFFSET, peer_address, sizeof(*peer_address), BPF_F_INVALIDATE_HASH)) {
		emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_CSUM_FAILED, debug_target, NULL);
		return -1;
	}

	if (bpf_skb_store_bytes(packet, ETH_HLEN + ATLAS_UNICAST_IPV4_CHECKSUM_OFFSET, &checksum, sizeof(checksum), 0)) {
		emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_CSUM_FAILED, debug_target, NULL);
		return -1;
	}

	if (wrapped_length < sizeof(struct iphdr)) return -1;

	/*
	 * bpf_fib_lookup() expects tot_len as the host-order L3 length.
	 * ifindex is the input L3 device; on success the helper replaces
	 * it with the selected egress device.
	 */
	route.family = ATLAS_UNICAST_AF_INET;
	route.tot_len = wrapped_length;
	route.ifindex = packet->ifindex;
	route.ipv4_src = source_address;
	route.ipv4_dst = *peer_address;

	fib_result = bpf_fib_lookup(packet, &route, sizeof(route), BPF_FIB_LOOKUP_OUTPUT);

	if (fib_result)
	{
		struct in6_addr fib_debug = {};

		fib_debug.s6_addr32[0] = bpf_htonl(fib_result);

		emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_FIB_FAILED, debug_target, &fib_debug);

		return fib_result;
	}

	if (bpf_skb_store_bytes(packet, 0, route.dmac, ETH_ALEN, 0)) {
		emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_DST_MAC_FAILED, debug_target, NULL);
		return -1;
	}

	if (bpf_skb_store_bytes(packet, ETH_ALEN, route.smac, ETH_ALEN, 0)) {
		emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_SRC_MAC_FAILED, debug_target, NULL);
		return -1;
	}

	redirect_result = bpf_clone_redirect(packet, route.ifindex, 0);

	if (redirect_result) {
		emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_REDIRECT_FAILED, debug_target, NULL);
		return redirect_result;
	}

	return 0;
}

/* Attached to TC egress on the uplink interface in a unicast environment.
 * The multicast NDP hook is not attached there, so this hook owns the whole
 * advertisement path.
 *
 * Solicitation: append the requester option, wrap the packet in IPv4, and
 * send one copy to the last known owner peer from vm_peer_map, or one copy
 * to every peer when no owner is known. The original multicast solicitation
 * is consumed.
 *
 * Advertisement: append the TLLAO and Atlas options for a local VM, wrap
 * the extended advertisement in IPv4, and send it to the peer recorded from
 * the requester option of the solicitation.
 */
SEC("tc")
int handle_ndp_unicast_egress(struct __sk_buff *packet)
{
	void *data = (void *)(long)packet->data;
	void *end = (void *)(long)packet->data_end;

	struct ethhdr *eth = data;
	struct ipv6hdr *ip6;
	struct ndp_message *message;

	struct in6_addr target = {};
	struct in6_addr destination = {};
	struct in6_addr host = {};

	struct config *local_config;

	__be32 *known_peer;
	__be32 *requester;

	__be32 present_requester = 0;
	__be32 requester_address = 0;

	__s64 base_sum = 0;

	__u16 new_payload_length;
	__u16 wrapped_length;

	__u32 index;

	/* Wrapped packets are IPv4, so clones pass through this hook unchanged. */
	if ((void *)(eth + 1) > end) return TC_ACT_OK;

	if (eth->h_proto != bpf_htons(ETH_P_IPV6)) return TC_ACT_OK;

	ip6 = (void *)(eth + 1);

	if ((void *)(ip6 + 1) > end) return TC_ACT_OK;

	if (ip6->nexthdr != IPPROTO_ICMPV6) return TC_ACT_OK;

	if (bpf_ntohs(ip6->payload_len) < sizeof(struct ndp_message)) return TC_ACT_OK;

	message = (void *)(ip6 + 1);

	if ((void *)(message + 1) > end) return TC_ACT_OK;

	target = message->target;
	destination = ip6->daddr;

	if (message->icmp.icmp6_type == NDISC_NEIGHBOUR_SOLICITATION) {
		/* Duplicate address detection uses the unspecified source and
		 * has no requester that could receive an answer. */
		if (!ip6->saddr.s6_addr32[0] && !ip6->saddr.s6_addr32[1] && !ip6->saddr.s6_addr32[2] && !ip6->saddr.s6_addr32[3]) return TC_ACT_OK;

		/* Only mesh solicitation is transported. */
		if (!is_virtual_machine_address(&target)) return TC_ACT_OK;

		/* A local target stays on the Linux path. */
		if (is_local_virtual_machine(&target)) return TC_ACT_OK;

		/* Do not transport a solicitation that already carries the
		 * requester option. This guards against a loop. */
		if (atlas_unicast_find_requester(packet, ip6, ETH_HLEN, &present_requester)) return TC_ACT_OK;

		local_config = get_config();

		if (!local_config) return TC_ACT_OK;

		if (!atlas_unicast_append_requester_option(packet, ip6, message, end, local_config->underlay_ip4, &new_payload_length)) {
			emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_APPEND_FAILED, &target, NULL);

			return TC_ACT_OK;
		}

		wrapped_length = sizeof(struct iphdr) + sizeof(struct ipv6hdr) + new_payload_length;

		if (!atlas_unicast_wrap_ipv4(packet)) {
			emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_WRAP_FAILED, &target, NULL);

			return TC_ACT_OK;
		}

		if (!atlas_unicast_store_base_header(packet, local_config->underlay_ip4, wrapped_length, &base_sum)) {
			emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_WRAP_FAILED, &target, NULL);

			return TC_ACT_OK;
		}

		known_peer = bpf_map_lookup_elem(&vm_peer_map, &target);

		if (known_peer) {
			/* A known owner receives the only copy. The entry is
			 * one shot: it is removed now, so a missing answer makes
			 * the next solicitation fan out to every peer. */
			if (atlas_unicast_send_to_peer(packet, known_peer, local_config->underlay_ip4, wrapped_length, base_sum, &target)) emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_SEND_FAILED, &target, NULL);
			else emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_KNOWN_PEER, &target, NULL);

			bpf_map_delete_elem(&vm_peer_map, &target);

			/* The multicast solicitation has no meaning on a routed underlay. */
			return TC_ACT_SHOT;
		}

				/* The map key is a separate stack variable, because its address is taken.
		 * No counter is carried across iterations, because a precise loop-carried
		 * value stops the verifier from pruning equal iteration states.
		 */
		for (index = 0; index < ATLAS_UNICAST_PEER_LIMIT; index++) {
			__u32 key = index;

			__be32 *peer_address = bpf_map_lookup_elem(&peer_list, &key);

			if (!peer_address || *peer_address == 0) continue;

			if (atlas_unicast_send_to_peer(packet, peer_address, local_config->underlay_ip4, wrapped_length, base_sum, &target)) {
				emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_SEND_FAILED, &target, NULL);
			}
		}

		emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_FAN_OUT, &target, NULL);

		/* The multicast solicitation has no meaning on a routed underlay. */
		return TC_ACT_SHOT;
	}

	if (message->icmp.icmp6_type == NDISC_NEIGHBOUR_ADVERTISEMENT) {
		/* Only an answer for a local VM is transported. */
		if (!is_virtual_machine_address(&target) || !is_local_virtual_machine(&target)) return TC_ACT_OK;

		requester = bpf_map_lookup_elem(&ndp_requesters, &destination);

		if (!requester) {
			emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_NO_REQUESTER, &target, NULL);

			return TC_ACT_OK;
		}

		local_config = get_config();

		if (!local_config) return TC_ACT_OK;

		/* The unicast hook owns the Atlas option, because the multicast
		 * NDP hook is not attached in a unicast environment. Do not
		 * append it twice. */
		if (!atlas_unicast_find_atlas_host(packet, ip6, ETH_HLEN, &host) && !atlas_unicast_add_atlas_option(packet, eth, ip6, message, local_config)) {
			emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_ATLAS_APPEND_FAILED, &target, NULL);

			return TC_ACT_OK;
		}

		wrapped_length = (__u16)(packet->len + sizeof(struct iphdr) - ETH_HLEN);

		if (!atlas_unicast_wrap_ipv4(packet)) {
			emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_WRAP_FAILED, &target, NULL);

			return TC_ACT_OK;
		}

		if (!atlas_unicast_store_base_header(packet, local_config->underlay_ip4, wrapped_length, &base_sum)) {
			emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_WRAP_FAILED, &target, NULL);

			return TC_ACT_OK;
		}

		requester_address = *requester;

		if (atlas_unicast_send_to_peer(packet, &requester_address, local_config->underlay_ip4, wrapped_length, base_sum, &target)) {
			emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_SEND_FAILED, &target, NULL);

			return TC_ACT_SHOT;
		}

		/* The requester entry is one shot. A later solicitation stores
		 * it again. */
		bpf_map_delete_elem(&ndp_requesters, &destination);

		emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_SEND, UNICAST_OPERATION_EGRESS_ADVERTISEMENT, &target, NULL);

		return TC_ACT_SHOT;
	}

	return TC_ACT_OK;
}

/* Attached to TC ingress on the uplink interface in a unicast environment.
 * The multicast NDP hook is not attached there, so this hook owns the whole
 * learning path.
 *
 * A wrapped NDP packet is accepted only when the outer IPv4 source is a
 * configured peer and the destination is the local underlay address. The
 * outer header is removed, and the inner packet continues to the Linux
 * neighbour discovery path.
 *
 * Solicitation: the requester option is validated against the peer list,
 * and the requester is recorded in ndp_requesters for the answer.
 *
 * Advertisement: the owning peer is recorded in vm_peer_map, the owner is
 * recorded in remote_vms, and the neighbour is registered through the
 * Atlas kfunc, so Linux NUD owns liveness exactly as in the multicast
 * environment.
 */
SEC("tc")
int handle_ndp_unicast_ingress(struct __sk_buff *packet)
{
	void *data = (void *)(long)packet->data;
	void *end = (void *)(long)packet->data_end;

	struct ethhdr *eth = data;
	struct iphdr *ip4;
	struct ipv6hdr *ip6;
	struct ndp_message *message;

	struct in6_addr target = {};
	struct in6_addr source = {};
	struct in6_addr host = {};

	struct config *local_config;

	__be32 requester_address = 0;

	__be16 ethernet_type = bpf_htons(ETH_P_IPV6);

	__u8 type;

	if ((void *)(eth + 1) > end) return TC_ACT_OK;

	if (eth->h_proto != bpf_htons(ETH_P_IP)) return TC_ACT_OK;

	ip4 = (void *)(eth + 1);

	if ((void *)(ip4 + 1) > end) return TC_ACT_OK;

	if (ip4->version != 4 || ip4->ihl != 5) return TC_ACT_OK;

	if (bpf_ntohs(ip4->tot_len) < sizeof(struct iphdr) + sizeof(struct ipv6hdr) + sizeof(struct ndp_message)) return TC_ACT_OK;

	if (ip4->protocol != IPPROTO_IPV6) return TC_ACT_OK;

	local_config = get_config();

	if (!local_config) return TC_ACT_OK;

	if (ip4->daddr != local_config->underlay_ip4) return TC_ACT_OK;

	/* Do not trust an address that is not a configured peer. */
	if (!atlas_unicast_is_known_peer(ip4->saddr)) {
		emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_RECEIVE, UNICAST_OPERATION_INGRESS_PEER_REJECTED, &target, NULL);

		return TC_ACT_OK;
	}

	ip6 = (void *)(ip4 + 1);

	if ((void *)(ip6 + 1) > end) return TC_ACT_OK;

	if (ip6->nexthdr != IPPROTO_ICMPV6) return TC_ACT_OK;

	if (bpf_ntohs(ip6->payload_len) < sizeof(struct ndp_message)) return TC_ACT_OK;

	message = (void *)(ip6 + 1);

	if ((void *)(message + 1) > end) return TC_ACT_OK;

	type = message->icmp.icmp6_type;
	target = message->target;
	source = ip6->saddr;

	if (!is_virtual_machine_address(&target)) return TC_ACT_OK;

	if (type == NDISC_NEIGHBOUR_SOLICITATION) {
		if (atlas_unicast_find_requester(packet, ip6, ETH_HLEN + sizeof(struct iphdr), &requester_address) && requester_address == ip4->saddr) {
			int map_result = bpf_map_update_elem(&ndp_requesters, &source, &requester_address, BPF_ANY);

			if (!map_result) emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_RECEIVE, UNICAST_OPERATION_INGRESS_REQUESTER_STORED, &target, NULL);
		}
	} else if (type == NDISC_NEIGHBOUR_ADVERTISEMENT) {
		/* Only a remote VM records a location. */
		if (!is_local_virtual_machine(&target) && atlas_unicast_find_atlas_host(packet, ip6, ETH_HLEN + sizeof(struct iphdr), &host) && is_underlay_address(&host)) {
			int peer_result = bpf_map_update_elem(&vm_peer_map, &target, &ip4->saddr, BPF_ANY);

			/* Mirror the multicast learning: record the owner and
			 * register the neighbour, so Linux NUD owns liveness.
			 * The unicast hook owns this step, because the
			 * multicast NDP hook is not attached here. */
			int location_result = bpf_map_update_elem(&remote_vms, &target, &host, BPF_ANY);

			if (!peer_result && !location_result) {
				__u64 addr_hi = 0;
				__u64 addr_lo = 0;
				__u64 mac = 0;

				__builtin_memcpy(&addr_hi, &target.s6_addr[0], sizeof(addr_hi));

				__builtin_memcpy(&addr_lo, &target.s6_addr[8], sizeof(addr_lo));

				/* The frame source MAC is the answering peer's
				 * uplink address, the same L2 address that the
				 * TLLAO advertises. */
				__builtin_memcpy(&mac, eth->h_source, ETH_ALEN);

				if (atlas_register_neigh(packet->ifindex, addr_hi, addr_lo, mac)) emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_RECEIVE, UNICAST_OPERATION_INGRESS_KFUNC_FAILED, &target, &host);
				else emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_RECEIVE, UNICAST_OPERATION_INGRESS_REMOTE_LEARNED, &target, &host);
			}
		} else {
			emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_RECEIVE, UNICAST_OPERATION_INGRESS_NO_ATLAS_OPTION, &target, NULL);
		}
	} else {
		return TC_ACT_OK;
	}

	emit_protocol_debug_event(DEBUG_UNICAST, DEBUG_RECEIVE, UNICAST_OPERATION_INGRESS_ACCEPTED, &target, NULL);

	/* Remove the outer IPv4 header. The flag also changes skb->protocol
	 * back to IPv6, so the Linux receive path picks the inner packet. */
	if (bpf_skb_adjust_room(packet, -(int)sizeof(struct iphdr), BPF_ADJ_ROOM_MAC, BPF_F_ADJ_ROOM_FIXED_GSO | BPF_F_ADJ_ROOM_DECAP_L3_IPV6)) return TC_ACT_SHOT;

	/* Restore the Ethernet type in the frame bytes. The decap flag already
	 * fixed skb->protocol for the Linux receive path; the frame bytes make
	 * the inner packet self consistent for any direct reader, including
	 * the multicast NDP hook during a start or stop window. */
	if (bpf_skb_store_bytes(packet, ATLAS_UNICAST_ETHERTYPE_OFFSET, &ethernet_type, sizeof(ethernet_type), 0)) return TC_ACT_SHOT;

	return TC_ACT_OK;
}

#endif /* ATLAS_NDP_UNICAST_HOOK_H */
