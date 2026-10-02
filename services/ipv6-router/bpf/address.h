/* SPDX-License-Identifier: AGPL-3.0 */
/* The address layouts and the stateless mapping between them.
 *
 *   public  = prefix, at most /80 | tenant 32 bits | virtual machine 16 bits
 *   private = fdaa | region 16 bits | tenant 32 bits | padding 48 bits, zero | virtual machine 16 bits
 */
#ifndef ATLAS_IPV6_ROUTER_ADDRESS_H
#define ATLAS_IPV6_ROUTER_ADDRESS_H

#include <stdbool.h>
#include <linux/in6.h>
#include <linux/types.h>
#include <bpf/bpf_endian.h>

/* setup.sh writes REGION_ID and the PUBLIC_* prefix words in host byte order. */
#include "config.h"

#define MESH_PREFIX 0xfdaaULL
#define MESH_REGION ((MESH_PREFIX << 16) | REGION_ID)

#define TENANT_BITS 32
#define MACHINE_BITS 16
#define TENANT_MASK ((1ULL << TENANT_BITS) - 1)
#define MACHINE_LIMIT (1ULL << MACHINE_BITS)
#define HOST_MASK ((1ULL << (TENANT_BITS + MACHINE_BITS)) - 1)

/* An IPv6 address as two 64-bit words in host byte order, so that masks and shifts are simple. */
struct address
{
	__u64 high;
	__u64 low;
};

static __always_inline struct address to_address(const struct in6_addr *raw)
{
	struct address address;

	address.high = ((__u64)bpf_ntohl(raw->in6_u.u6_addr32[0]) << 32) | bpf_ntohl(raw->in6_u.u6_addr32[1]);
	address.low = ((__u64)bpf_ntohl(raw->in6_u.u6_addr32[2]) << 32) | bpf_ntohl(raw->in6_u.u6_addr32[3]);
	return address;
}

static __always_inline struct in6_addr to_raw(const struct address *address)
{
	struct in6_addr raw;

	raw.in6_u.u6_addr32[0] = bpf_htonl(address->high >> 32);
	raw.in6_u.u6_addr32[1] = bpf_htonl((__u32)address->high);
	raw.in6_u.u6_addr32[2] = bpf_htonl(address->low >> 32);
	raw.in6_u.u6_addr32[3] = bpf_htonl((__u32)address->low);
	return raw;
}

/* Any WG Mesh address, in any region. */
static __always_inline bool is_mesh(const struct address *address)
{
	return (address->high >> 48) == MESH_PREFIX;
}

/* 2000::/3. This excludes link-local, multicast, and mesh destinations. */
static __always_inline bool is_global_unicast(const struct address *address)
{
	return (address->high >> 61) == 1;
}

static __always_inline bool is_in_block(const struct address *address)
{
	return (address->high & PUBLIC_HIGH_MASK) == PUBLIC_HIGH && (address->low & PUBLIC_LOW_MASK) == PUBLIC_LOW;
}

/* A block address maps to a virtual machine only when the bits between the block and the host part are zero. */
static __always_inline bool is_mapped_public(const struct address *address)
{
	return address->high == PUBLIC_HIGH && (address->low & ~HOST_MASK) == PUBLIC_LOW;
}

/* Call only for an address that is_mapped_public accepts. */
static __always_inline struct address to_private(const struct address *public)
{
	struct address private;

	private.high = (MESH_REGION << 32) | ((public->low >> MACHINE_BITS) & TENANT_MASK);
	private.low = public->low & (MACHINE_LIMIT - 1);
	return private;
}

/* Returns false for another region, or an address with padding, such as the Atlas address. */
static __always_inline bool to_public(const struct address *private, struct address *public)
{
	__u64 tenant = private->high & TENANT_MASK;

	if ((private->high >> 32) != MESH_REGION || private->low >= MACHINE_LIMIT)
		return false;

	public->high = PUBLIC_HIGH;
	public->low = PUBLIC_LOW | (tenant << MACHINE_BITS) | private->low;
	return true;
}

#endif
