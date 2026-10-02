/* SPDX-License-Identifier: AGPL-3.0 */
/* Addresses, wire formats, and packet helpers shared by every hook. */
#ifndef ATLAS_MESH_H
#define ATLAS_MESH_H

#include <linux/bpf.h>
#include <linux/icmpv6.h>
#include <linux/if_ether.h>
#include <linux/in.h>
#include <linux/ip.h>
#include <linux/ipv6.h>
#include <linux/pkt_cls.h>

#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

/* VM address: fdaa | region 16 | tenant 32 | padding 48 | VM 16. */
#define VM_PREFIX 0xfdaa
/* Host WireGuard address. */
#define UNDERLAY_PREFIX 0xfdab

#define NDP_SOLICITATION 135
#define NDP_ADVERTISEMENT 136
#define NDP_SOURCE_MAC_OPTION 1
#define NDP_TARGET_MAC_OPTION 2

/* NOT_HERE travels inside WireGuard as an experimental IPv6 next header. Its payload is the VM address. */
#define NOT_HERE_NEXT_HEADER 253
#define NOT_HERE_PACKET_LENGTH (sizeof(struct ipv6hdr) + sizeof(struct in6_addr))

/* A tunnel to a gateway carries the gateway address before the client packet, so a host can run several gateways. */
#define GATEWAY_NEXT_HEADER 254

#define HOP_LIMIT 64
#define AF_INET6 10

struct ndp_message
{
	struct icmp6hdr icmp;
	struct in6_addr target;
};

/* A solicitation with the source MAC option, or an advertisement with the target MAC option. */
struct ndp_with_mac
{
	struct icmp6hdr icmp;
	struct in6_addr target;
	__u8 option_type;
	__u8 option_length;
	__u8 mac[ETH_ALEN];
};

/* The part of the IPv6 header that the ICMPv6 checksum covers. */
struct pseudo_header
{
	struct in6_addr source;
	struct in6_addr destination;
	__be32 length;
	__u8 zero[3];
	__u8 next_header;
};

/* Address policy. */
static __always_inline __u16 ipv6_prefix(const struct in6_addr *address)
{
	return bpf_ntohs(address->s6_addr16[0]);
}

static __always_inline int is_vm_address(const struct in6_addr *address)
{
	return ipv6_prefix(address) == VM_PREFIX;
}

static __always_inline int is_underlay_address(const struct in6_addr *address)
{
	return ipv6_prefix(address) == UNDERLAY_PREFIX;
}

/* True for an address a gateway can carry. The underlay, link-local, and multicast stay with the host. */
static __always_inline int is_gateway_carried_address(const struct in6_addr *address)
{
	__u16 prefix = ipv6_prefix(address);

	return prefix != UNDERLAY_PREFIX && (prefix & 0xffc0) != 0xfe80 && (prefix >> 8) != 0xff;
}

static __always_inline __be32 tenant_id(const struct in6_addr *address)
{
	return address->s6_addr32[1];
}

static __always_inline int is_same_address(const struct in6_addr *left, const struct in6_addr *right)
{
	return left->s6_addr32[0] == right->s6_addr32[0] && left->s6_addr32[1] == right->s6_addr32[1] &&
		   left->s6_addr32[2] == right->s6_addr32[2] && left->s6_addr32[3] == right->s6_addr32[3];
}

static __always_inline int is_unspecified_address(const struct in6_addr *address)
{
	return is_same_address(address, &(struct in6_addr){});
}

/* Pack a MAC into the low 6 bytes of a u64 so it compares in one step. */
static __always_inline __u64 pack_mac(const __u8 *mac)
{
	__u64 value = 0;

	__builtin_memcpy(&value, mac, ETH_ALEN);

	return value;
}

static __always_inline __u16 fold_checksum(__u64 sum)
{
	sum = (sum & 0xffff) + (sum >> 16);
	sum = (sum & 0xffff) + (sum >> 16);

	return (__u16)~sum;
}

/* NDP packet creation. The caller supplies complete Ethernet, IPv6, and message values. */
static __always_inline int replace_with_ndp(struct __sk_buff *packet, struct ethhdr *eth, struct ipv6hdr *ip6, struct ndp_with_mac *message)
{
	struct pseudo_header pseudo = {
		.source = ip6->saddr,
		.destination = ip6->daddr,
		.length = bpf_htonl(sizeof(*message)),
		.next_header = IPPROTO_ICMPV6,
	};
	__s64 sum = bpf_csum_diff(NULL, 0, (__be32 *)&pseudo, sizeof(pseudo), 0);
	__s64 checksum;

	if (sum < 0)
		return -1;

	ip6->version = 6;
	ip6->payload_len = bpf_htons(sizeof(*message));
	ip6->nexthdr = IPPROTO_ICMPV6;
	ip6->hop_limit = 255;
	message->option_length = 1;
	message->icmp.icmp6_cksum = 0;
	checksum = bpf_csum_diff(NULL, 0, (__be32 *)message, sizeof(*message), (__wsum)sum);
	if (checksum < 0)
		return -1;
	message->icmp.icmp6_cksum = fold_checksum(checksum);

	return bpf_skb_change_tail(packet, ETH_HLEN + sizeof(*ip6) + sizeof(*message), 0) ||
		   bpf_skb_store_bytes(packet, 0, eth, sizeof(*eth), BPF_F_INVALIDATE_HASH) ||
		   bpf_skb_store_bytes(packet, ETH_HLEN, ip6, sizeof(*ip6), BPF_F_INVALIDATE_HASH) ||
		   bpf_skb_store_bytes(packet, ETH_HLEN + sizeof(*ip6), message, sizeof(*message), BPF_F_INVALIDATE_HASH);
}

/* Replace the packet with a neighbor solicitation for the target. */
static __always_inline int replace_with_neighbor_solicitation(struct __sk_buff *packet, const __u8 *mac, const struct in6_addr *source, const struct in6_addr *target)
{
	struct ethhdr eth = {.h_dest = {0x33, 0x33, 0xff}, .h_proto = bpf_htons(ETH_P_IPV6)};
	struct ipv6hdr ip6 = {.saddr = *source, .daddr.s6_addr = {0xff, 0x02, [11] = 0x01, [12] = 0xff}};
	struct ndp_with_mac solicitation = {
		.icmp.icmp6_type = NDP_SOLICITATION,
		.target = *target,
		.option_type = NDP_SOURCE_MAC_OPTION,
	};

	/* The solicited-node address and its MAC end with the last 3 bytes of the target. */
	__builtin_memcpy(&eth.h_dest[3], &target->s6_addr[13], 3);
	__builtin_memcpy(&ip6.daddr.s6_addr[13], &target->s6_addr[13], 3);
	__builtin_memcpy(eth.h_source, mac, ETH_ALEN);
	__builtin_memcpy(solicitation.mac, mac, ETH_ALEN);

	return replace_with_ndp(packet, &eth, &ip6, &solicitation);
}

/* NDP fits in the minimum IPv6 MTU. The cap also gives the verifier the upper
 * bound that the packet length alone cannot prove. */
#define NDP_MAX_PAYLOAD (1280 - sizeof(struct ipv6hdr))

/* Packet parsing. Each function returns NULL for traffic that is not its protocol. */
static __always_inline struct ipv6hdr *parse_ethernet_ipv6(void *data, void *end)
{
	struct ethhdr *eth = data;
	struct ipv6hdr *ip6 = (void *)(eth + 1);

	if ((void *)(ip6 + 1) > end || eth->h_proto != bpf_htons(ETH_P_IPV6) || ip6->version != 6)
		return NULL;

	return ip6;
}

static __always_inline struct ndp_message *parse_ndp(struct ipv6hdr *ip6, void *end)
{
	struct ndp_message *message = (void *)(ip6 + 1);
	__u16 payload_length;

	/* The bounds check comes first, so the verifier sees every header read inside the packet. */
	if ((void *)(message + 1) > end || ip6->nexthdr != IPPROTO_ICMPV6)
		return NULL;

	payload_length = bpf_ntohs(ip6->payload_len);
	if (payload_length < sizeof(*message) || payload_length > NDP_MAX_PAYLOAD ||
		(void *)message + payload_length > end || ip6->hop_limit != 255 ||
		message->icmp.icmp6_code != 0 ||
		(message->icmp.icmp6_type != NDP_SOLICITATION && message->icmp.icmp6_type != NDP_ADVERTISEMENT) ||
		message->target.s6_addr[0] == 0xff)
		return NULL;

	return message;
}

#endif /* ATLAS_MESH_H */
