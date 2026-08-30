/* SPDX-License-Identifier: AGPL-3.0 */
/*
 * Atlas WG Mesh protocol definitions.
 *
 * This file contains the values and structures that must match on every
 * Atlas WG Mesh host. Changing atlas_msg changes the on-the-wire protocol.
 */
#ifndef ATLAS_PROTOCOL_H
#define ATLAS_PROTOCOL_H

#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/in.h>
#include <linux/ip.h>
#include <linux/ipv6.h>
#include <linux/pkt_cls.h>
#include <linux/udp.h>

#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

#define ATLAS_VER 1

#define MESSAGE_OPERATION_WHO_HAS 1
#define MESSAGE_OPERATION_FOUND 2
#define MESSAGE_OPERATION_NOT_HERE 3
#define MESSAGE_OPERATION_NOW_HERE 4

/* Discovery uses IPv4 multicast on the local physical network. */
#define ATLAS_MCAST4 0xef010101u /* 239.1.1.1 */
#define ATLAS_PORT 7373
#define ATLAS_MULTICAST_TTL 1

/* A unicast reply has no multicast scope to keep and must survive routers. */
#define ATLAS_UNICAST_TTL 64

/* Recovery travels inside WireGuard as an experimental IPv6 next header. */
#define ATLAS_CONTROL_NEXT_HEADER 253

/*
 * VM address layout: fd aa | region 16 | tenant 32 | reserved 32 | VM ID 32.
 * The tenant occupies bytes 4 to 7.
 */
#define VM_PREFIX0 0xfd
#define VM_PREFIX1 0xaa

/*
 * Host WireGuard addresses live in fdab::/16.
 */
#define UNDERLAY_PREFIX0 0xfd
#define UNDERLAY_PREFIX1 0xab

struct atlas_msg
{
	__u8 ver;
	__u8 op;
	__u8 pad[2];
	struct in6_addr vm;
	struct in6_addr host;
}; /* 36 bytes, no padding */

/* Byte offsets in an Ethernet, IPv4, and UDP discovery packet. */
#define DISCOVERY_IPV4_HEADER_OFFSET ETH_HLEN
#define DISCOVERY_UDP_HEADER_OFFSET (DISCOVERY_IPV4_HEADER_OFFSET + sizeof(struct iphdr))
#define DISCOVERY_MESSAGE_OFFSET (DISCOVERY_UDP_HEADER_OFFSET + sizeof(struct udphdr))
#define DISCOVERY_PACKET_LENGTH (DISCOVERY_MESSAGE_OFFSET + sizeof(struct atlas_msg))

/* WireGuard control packets have no Ethernet header. */
#define WIREGUARD_CONTROL_MESSAGE_OFFSET sizeof(struct ipv6hdr)
#define WIREGUARD_CONTROL_PACKET_LENGTH (WIREGUARD_CONTROL_MESSAGE_OFFSET + sizeof(struct atlas_msg))

#endif /* ATLAS_PROTOCOL_H */
