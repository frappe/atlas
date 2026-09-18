/* SPDX-License-Identifier: AGPL-3.0 */
/* TC hook for the shared VLAN interface: Atlas NDP handling. */
#ifndef ATLAS_NDP_HOOK_H
#define ATLAS_NDP_HOOK_H

#include "debug.h"
#include "state.h"

/* Byte offsets inside the IPv6 header and the ICMPv6 header. */
#define IPV6_PAYLOAD_LENGTH_OFFSET 4
#define ICMPV6_CHECKSUM_OFFSET 2

/* Limit the option walk, so the verifier can bound the loop. */
#define NDP_OPTION_WALK_LIMIT 16

/* Standard NDP Target Link-Layer Address option. */
#define ATLAS_TLLAO_TYPE 2
#define ATLAS_TLLAO_LENGTH 1

#define NDP_OPERATION_APPEND_NO_CONFIG            7
#define NDP_OPERATION_APPEND_TOO_LARGE            8
#define NDP_OPERATION_APPEND_TAIL_FAILED          9
#define NDP_OPERATION_APPEND_STORE_FAILED        10
#define NDP_OPERATION_APPEND_LENGTH_FAILED       11
#define NDP_OPERATION_APPEND_READ_FAILED         12
#define NDP_OPERATION_APPEND_CSUM_FAILED         13
#define NDP_OPERATION_APPEND_CSUM_STORE_FAILED   14
#define NDP_OPERATION_APPEND_CSUM_REPLACE_FAILED 15
#define NDP_OPERATION_APPEND_SUCCEEDED           16

/* Receive-path debug operations: one per decision point in handle_ndp_packet(). */
#define NDP_OPERATION_RX_START                  17
#define NDP_OPERATION_RX_NOT_IPV6               18
#define NDP_OPERATION_RX_NOT_ICMPV6             19
#define NDP_OPERATION_RX_SHORT_MESSAGE          20
#define NDP_OPERATION_RX_SOLICITATION           21
#define NDP_OPERATION_RX_NOT_ADVERTISEMENT      22
#define NDP_OPERATION_RX_NOT_VM                 23
#define NDP_OPERATION_RX_LOCAL_VM               24
#define NDP_OPERATION_RX_LOCAL_ATLAS_PRESENT   25
#define NDP_OPERATION_RX_REMOTE_VM              26
#define NDP_OPERATION_RX_ATLAS_MISSING          27
#define NDP_OPERATION_RX_HOST_INVALID           28
#define NDP_OPERATION_RX_MAP_UPDATE             29
#define NDP_OPERATION_RX_MAP_UPDATE_FAILED      30
#define NDP_OPERATION_RX_KFUNC_CALL             31
#define NDP_OPERATION_RX_KFUNC_FAILED           32
#define NDP_OPERATION_RX_KFUNC_SUCCEEDED        33

/* Atlas neighbour kfunc, provided by the Atlas kernel module.
 * addr_hi/addr_lo carry the first/last 8 bytes of the IPv6 address,
 * and the MAC is packed into the low 6 bytes of mac.
 */
extern int atlas_register_neigh(
	__u32 ifindex,
	__u64 addr_hi,
	__u64 addr_lo,
	__u64 mac) __ksym;

/* IPv6 pseudo-header used for ICMPv6 checksum calculation. RFC 8200, 40 bytes. */
struct atlas_ipv6_pseudo_header {
	struct in6_addr saddr;
	struct in6_addr daddr;
	__be32 length;
	__u8 zero[3];
	__u8 nexthdr;
};

/* Standard NDP Target Link-Layer Address option, 8 bytes on the wire. */
struct atlas_tllao {
	__u8 type;
	__u8 length;
	__u8 mac[ETH_ALEN];
};

/* Everything appended to the Neighbor Advertisement: TLLAO plus the Atlas
 * option, which carries the WireGuard address of the owning host.
 */
struct atlas_ndp_append {
	struct atlas_tllao tllao;
	struct atlas_ndp_option atlas;
};

/* Fold a one's-complement checksum down to 16 bits. */
static __always_inline __u16 atlas_csum_fold(__u64 sum)
{
	sum = (sum & 0xffffffff) + (sum >> 32);
	sum = (sum & 0xffffffff) + (sum >> 32);

	sum = (sum & 0xffff) + (sum >> 16);
	sum = (sum & 0xffff) + (sum >> 16);

	return (__u16)~sum;
}

/* Walk the NDP options and copy the Atlas host address into a stack object.
 * Do not return a packet pointer: packet pointers inside a bounded loop
 * make verifier packet-range tracking fragile.
 */
static __always_inline int find_atlas_host(
	struct __sk_buff *packet,
	struct ndp_message *message,
	void *end,
	struct in6_addr *host)
{
	__u8 *cursor = (__u8 *)(message + 1);
	__u8 *limit = end;
	__u8 type;
	__u8 length;
	__u32 option_offset;
	int step;

	for (step = 0; step < NDP_OPTION_WALK_LIMIT; step++) {
		if (cursor + 2 > limit)
			return 0;

		type = cursor[0];
		length = cursor[1];

		if (length == 0)
			return 0;

		if (cursor + length * 8 > limit)
			return 0;

		if (type == ATLAS_NDP_OPTION_TYPE) {
			if (length != ATLAS_NDP_OPTION_LENGTH + 1)
				return 0;

			option_offset = ETH_HLEN + sizeof(struct ipv6hdr) + sizeof(struct ndp_message) + (cursor - (__u8 *)(message + 1)) + 4;

			if (bpf_skb_load_bytes(packet, option_offset, host, sizeof(*host)))
				return 0;

			return 1;
		}

		cursor += length * 8;
	}

	return 0;
}

/* Append the TLLAO and Atlas options to a Neighbor Advertisement, then
 * recalculate the ICMPv6 checksum from scratch over the pseudo-header, the
 * complete message, and the appended options.
 *
 * bpf_skb_change_tail() invalidates packet pointers. Every packet-derived
 * value needed after the resize is copied to stack memory first.
 */
static __always_inline int add_atlas_option(
	struct __sk_buff *packet,
	const struct ethhdr *eth,
	const struct ipv6hdr *ip6,
	const struct ndp_message *message)
{
	struct atlas_ndp_append append = {};
	struct atlas_ipv6_pseudo_header pseudo = {};
	struct ndp_message final_message = {};

	struct in6_addr source;
	struct in6_addr destination;
	struct in6_addr target;
	struct in6_addr host;

	__be16 new_checksum;
	__be16 wire_payload_length;

	__u16 old_payload_length;
	__u16 new_payload_length;

	__u32 append_offset;
	__u32 icmp_offset;
	__u32 checksum_offset;
	__u32 append_length;

	__s64 icmp_sum;
	__s64 pseudo_sum;
	__u64 total_sum;

	struct config *local_config;

	local_config = get_config();

	if (!local_config) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_SEND, NDP_OPERATION_APPEND_NO_CONFIG, &message->target, NULL);
		return TC_ACT_OK;
	}

	append.tllao.type = ATLAS_TLLAO_TYPE;
	append.tllao.length = ATLAS_TLLAO_LENGTH;

	__builtin_memcpy(append.tllao.mac, eth->h_source, ETH_ALEN);

	append.atlas.type = ATLAS_NDP_OPTION_TYPE;
	append.atlas.length = ATLAS_NDP_OPTION_LENGTH + 1;
	append.atlas.host = local_config->wg_ip6;

	source = ip6->saddr;
	destination = ip6->daddr;
	target = message->target;
	host = local_config->wg_ip6;

	append_length = sizeof(append);

	if (append_length != sizeof(struct atlas_tllao) + sizeof(struct atlas_ndp_option)) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_SEND, NDP_OPERATION_APPEND_TOO_LARGE, &target, &host);
		return TC_ACT_OK;
	}

	old_payload_length = bpf_ntohs(ip6->payload_len);

	/* IPv6 Payload Length is 16 bits. */
	if ((__u32)old_payload_length + append_length > 0xffff) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_SEND, NDP_OPERATION_APPEND_TOO_LARGE, &target, &host);
		return TC_ACT_OK;
	}

	new_payload_length = old_payload_length + append_length;

	append_offset = ETH_HLEN + sizeof(struct ipv6hdr) + sizeof(struct ndp_message);
	icmp_offset = ETH_HLEN + sizeof(struct ipv6hdr);
	checksum_offset = icmp_offset + ICMPV6_CHECKSUM_OFFSET;

	emit_protocol_debug_event(DEBUG_NDP, DEBUG_SEND, NDP_OPERATION_ANNOUNCE, &target, &host);

	if (bpf_skb_change_tail(packet, packet->len + append_length, 0)) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_SEND, NDP_OPERATION_APPEND_TAIL_FAILED, &target, &host);
		return TC_ACT_OK;
	}

	if (bpf_skb_store_bytes(packet, append_offset, &append, append_length, 0)) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_SEND, NDP_OPERATION_APPEND_STORE_FAILED, &target, &host);
		return TC_ACT_OK;
	}

	wire_payload_length = bpf_htons(new_payload_length);

	if (bpf_skb_store_bytes(packet, ETH_HLEN + IPV6_PAYLOAD_LENGTH_OFFSET, &wire_payload_length, sizeof(wire_payload_length), 0)) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_SEND, NDP_OPERATION_APPEND_LENGTH_FAILED, &target, &host);
		return TC_ACT_OK;
	}

	/* No direct packet access after bpf_skb_change_tail(). */
	if (bpf_skb_load_bytes(packet, icmp_offset, &final_message, sizeof(final_message))) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_SEND, NDP_OPERATION_APPEND_READ_FAILED, &target, &host);
		return TC_ACT_OK;
	}

	/* The checksum field must be zero while calculating the new checksum. */
	final_message.icmp.icmp6_cksum = 0;

	icmp_sum = bpf_csum_diff(NULL, 0, (__be32 *)&final_message, sizeof(final_message), 0);

	if (icmp_sum < 0) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_SEND, NDP_OPERATION_APPEND_CSUM_FAILED, &target, &host);
		return TC_ACT_OK;
	}

	icmp_sum = bpf_csum_diff(NULL, 0, (__be32 *)&append, sizeof(append), (__wsum)icmp_sum);

	if (icmp_sum < 0) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_SEND, NDP_OPERATION_APPEND_CSUM_FAILED, &target, &host);
		return TC_ACT_OK;
	}

	__builtin_memset(&pseudo, 0, sizeof(pseudo));

	pseudo.saddr = source;
	pseudo.daddr = destination;
	pseudo.length = bpf_htonl(new_payload_length);
	pseudo.nexthdr = IPPROTO_ICMPV6;

	pseudo_sum = bpf_csum_diff(NULL, 0, (__be32 *)&pseudo, sizeof(pseudo), 0);

	if (pseudo_sum < 0) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_SEND, NDP_OPERATION_APPEND_CSUM_FAILED, &target, &host);
		return TC_ACT_OK;
	}

	total_sum = (__u32)icmp_sum + (__u32)pseudo_sum;

	/* Do NOT call bpf_htons() here, and do not use bpf_l4_csum_replace():
	 * new_checksum is already the final checksum, and that helper performs
	 * an incremental update based on a changed L4 field.
	 */
	new_checksum = atlas_csum_fold(total_sum);

	if (bpf_skb_store_bytes(packet, checksum_offset, &new_checksum, sizeof(new_checksum), 0)) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_SEND, NDP_OPERATION_APPEND_CSUM_STORE_FAILED, &target, &host);
		return TC_ACT_OK;
	}

	emit_protocol_debug_event(DEBUG_NDP, DEBUG_SEND, NDP_OPERATION_APPEND_SUCCEEDED, &target, &host);

	return TC_ACT_OK;
}

/* Attached to TC ingress and egress on the shared VLAN interface.
 *
 * Egress: an NA for a local VM is a proxy NDP answer, so append the TLLAO
 * with this interface's MAC and the Atlas option with this host's WireGuard
 * IPv6 address.
 *
 * Ingress: an NA with an Atlas option identifies the owner of a remote VM.
 * Record that mapping and register the VM in the Linux neighbour table
 * through the Atlas kfunc.
 */
SEC("tc")
int handle_ndp_packet(struct __sk_buff *packet)
{
	void *data = (void *)(long)packet->data;
	void *end = (void *)(long)packet->data_end;

	struct ethhdr *eth = data;
	struct ipv6hdr *ip6;
	struct ndp_message *message;

	struct in6_addr target = {};
	struct in6_addr host = {};

	__u64 addr_hi = 0;
	__u64 addr_lo = 0;
	__u64 mac = 0;

	int map_result;
	int kfunc_result;

	emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_START, &target, NULL);

	if ((void *)(eth + 1) > end) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_NOT_IPV6, &target, NULL);
		return TC_ACT_OK;
	}

	if (eth->h_proto != bpf_htons(ETH_P_IPV6)) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_NOT_IPV6, &target, NULL);
		return TC_ACT_OK;
	}

	ip6 = (void *)(eth + 1);

	if ((void *)(ip6 + 1) > end) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_NOT_ICMPV6, &target, NULL);
		return TC_ACT_OK;
	}

	if (ip6->nexthdr != IPPROTO_ICMPV6) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_NOT_ICMPV6, &target, NULL);
		return TC_ACT_OK;
	}

	message = (void *)(ip6 + 1);

	if ((void *)(message + 1) > end) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_SHORT_MESSAGE, &target, NULL);
		return TC_ACT_OK;
	}

	target = message->target;

	if (message->icmp.icmp6_type == NDISC_NEIGHBOUR_SOLICITATION) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_SOLICITATION, &target, NULL);
		return TC_ACT_OK;
	}

	if (message->icmp.icmp6_type != NDISC_NEIGHBOUR_ADVERTISEMENT) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_NOT_ADVERTISEMENT, &target, NULL);
		return TC_ACT_OK;
	}

	/* Ignore normal NDP for non-VM addresses. */
	if (!is_virtual_machine_address(&target)) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_NOT_VM, &target, NULL);
		return TC_ACT_OK;
	}

	/* Linux generated an NA for a VM owned by this host. */
	if (is_local_virtual_machine(&target)) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_LOCAL_VM, &target, NULL);

		/* Don't append the Atlas option twice. */
		if (find_atlas_host(packet, message, end, &host)) {
			emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_LOCAL_ATLAS_PRESENT, &target, &host);
			return TC_ACT_OK;
		}

		return add_atlas_option(packet, eth, ip6, message);
	}

	emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_REMOTE_VM, &target, NULL);

	/* Only Atlas advertisements contain the owner information we need. */
	if (!find_atlas_host(packet, message, end, &host)) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_ATLAS_MISSING, &target, NULL);
		return TC_ACT_OK;
	}

	/* Only accept a valid Atlas underlay address. */
	if (!is_underlay_address(&host)) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_HOST_INVALID, &target, &host);
		return TC_ACT_OK;
	}

	emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_LEARN, &target, &host);

	/* BPF_ANY intentionally allows the owner to be replaced if a later
	 * advertisement for the same VM arrives from a different Atlas host.
	 */
	map_result = bpf_map_update_elem(&remote_vms, &target, &host, BPF_ANY);

	if (map_result) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_MAP_UPDATE_FAILED, &target, &host);
		return TC_ACT_OK;
	}

	emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_MAP_UPDATE, &target, &host);

	/* Pack the IPv6 address into two scalar 64-bit values and the source
	 * MAC into the low six bytes of mac, as the kfunc expects.
	 */
	__builtin_memcpy(&addr_hi, &target.s6_addr[0], sizeof(addr_hi));
	__builtin_memcpy(&addr_lo, &target.s6_addr[8], sizeof(addr_lo));
	__builtin_memcpy(&mac, eth->h_source, ETH_ALEN);

	emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_KFUNC_CALL, &target, &host);

	kfunc_result = atlas_register_neigh(packet->ifindex, addr_hi, addr_lo, mac);

	if (kfunc_result) {
		emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_KFUNC_FAILED, &target, &host);
		return TC_ACT_OK;
	}

	emit_protocol_debug_event(DEBUG_NDP, DEBUG_RECEIVE, NDP_OPERATION_RX_KFUNC_SUCCEEDED, &target, &host);

	return TC_ACT_OK;
}

#endif /* ATLAS_NDP_HOOK_H */
