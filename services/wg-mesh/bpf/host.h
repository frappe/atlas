/* SPDX-License-Identifier: AGPL-3.0 */
/* TC ingress on the host veth: send the host's own traffic to a remote VM through WireGuard. */
#ifndef ATLAS_HOST_H
#define ATLAS_HOST_H

#include "vm.h"

SEC("tc")
int handle_host_packet(struct __sk_buff *packet)
{
	void *end = (void *)(long)packet->data_end;
	struct ipv6hdr *ip6 = parse_ethernet_ipv6((void *)(long)packet->data, end);
	struct bpf_redir_neigh next_hop = {.nh_family = AF_INET6};
	struct config *config = get_config();
	struct in6_addr destination;
	__u32 inner_length;

	/* Neighbor discovery for the veth gateway stays with Linux. */
	if (!ip6 || !is_vm_address(&ip6->daddr))
		return TC_ACT_OK;

	inner_length = bpf_ntohs(ip6->payload_len) + sizeof(*ip6);
	if (!config || inner_length > packet->len - ETH_HLEN || inner_length > 0xffff)
		return TC_ACT_SHOT;

	destination = ip6->daddr;
	if (is_same_address(&ip6->saddr, &config->wireguard_ipv6))
		return tunnel_to_remote_vm(packet, &destination, inner_length, NULL);

	/* Forwarded traffic, such as from the Atlas controller, keeps the plain uplink path. */
	__builtin_memcpy(next_hop.ipv6_nh, &destination, sizeof(next_hop.ipv6_nh));

	return bpf_redirect_neigh(config->uplink_ifindex, &next_hop, sizeof(next_hop), 0);
}

#endif /* ATLAS_HOST_H */
