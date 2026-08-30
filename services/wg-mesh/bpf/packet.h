/* SPDX-License-Identifier: AGPL-3.0 */
/* Atlas WG Mesh packet helpers. */
#ifndef ATLAS_PACKET_H
#define ATLAS_PACKET_H

#include "protocol.h"

static __always_inline __u16 calculate_ipv4_checksum(struct iphdr *ipv4_header)
{
	__u16 *word = (__u16 *)ipv4_header;
	__u32 sum = 0;
	int i;

	ipv4_header->check = 0;

#pragma unroll
	for (i = 0; i < (int)sizeof(*ipv4_header) / 2; i++)
		sum += word[i];

	sum = (sum & 0xffff) + (sum >> 16);
	sum = (sum & 0xffff) + (sum >> 16);
	return ~sum;
}

/* Set the packet length and clear encapsulation checksum state. */
static __always_inline int set_packet_length(struct __sk_buff *packet,
											 __u32 packet_length)
{
	if (bpf_skb_change_tail(packet, packet_length, 0))
		return -1;
	bpf_csum_level(packet, BPF_CSUM_LEVEL_RESET);
	return 0;
}

#endif /* ATLAS_PACKET_H */
