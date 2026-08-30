/* SPDX-License-Identifier: AGPL-3.0 */
/* Atlas WG Mesh IPv6 address helpers. */
#ifndef ATLAS_ADDRESS_H
#define ATLAS_ADDRESS_H

#include "protocol.h"

static __always_inline int is_virtual_machine_address(const struct in6_addr *ipv6_address)
{
	return ipv6_address->s6_addr[0] == VM_PREFIX0 &&
		   ipv6_address->s6_addr[1] == VM_PREFIX1;
}

/* True for a host WireGuard address. The mesh underlay is not guest reachable. */
static __always_inline int is_underlay_address(const struct in6_addr *ipv6_address)
{
	return ipv6_address->s6_addr[0] == UNDERLAY_PREFIX0 &&
		   ipv6_address->s6_addr[1] == UNDERLAY_PREFIX1;
}

static __always_inline __be32 get_tenant(const struct in6_addr *ipv6_address)
{
	return ipv6_address->s6_addr32[1];
}

static __always_inline int tenants_can_communicate(const struct in6_addr *source, const struct in6_addr *destination)
{
	/* Tenant zero is reserved for trusted shared services such as proxies. */
	return !get_tenant(source) || !get_tenant(destination) ||
		   get_tenant(source) == get_tenant(destination);
}

static __always_inline int are_ipv6_addresses_equal(const struct in6_addr *left, const struct in6_addr *right)
{
	return left->s6_addr32[0] == right->s6_addr32[0] &&
		   left->s6_addr32[1] == right->s6_addr32[1] &&
		   left->s6_addr32[2] == right->s6_addr32[2] &&
		   left->s6_addr32[3] == right->s6_addr32[3];
}

#endif /* ATLAS_ADDRESS_H */
