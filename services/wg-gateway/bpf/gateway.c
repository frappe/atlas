/* SPDX-License-Identifier: AGPL-3.0 */
#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ipv6.h>
#include <linux/pkt_cls.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

static __always_inline int valid_client(const struct in6_addr *client, const struct in6_addr *vm)
{
	return bpf_ntohs(client->s6_addr16[0]) == 0xfdac &&
	       bpf_ntohs(vm->s6_addr16[0]) == 0xfdaa &&
	       bpf_ntohs(client->s6_addr16[1]) == REGION_ID &&
	       bpf_ntohs(vm->s6_addr16[1]) == REGION_ID &&
	       bpf_ntohs(client->s6_addr16[2]) == GATEWAY_ID &&
	       client->s6_addr16[3] == vm->s6_addr16[2] &&
	       client->s6_addr16[4] == vm->s6_addr16[3] &&
	       (client->s6_addr16[3] || client->s6_addr16[4]) &&
	       (client->s6_addr16[5] || client->s6_addr16[6]) &&
	       client->s6_addr16[7] == 0;
}

SEC("tc/client")
int gateway_client_ingress(struct __sk_buff *skb)
{
	void *data = (void *)(long)skb->data;
	void *end = (void *)(long)skb->data_end;
	struct ipv6hdr *ip6 = data;

	if (bpf_ntohs(skb->protocol) != ETH_P_IPV6 || (void *)(ip6 + 1) > end)
		return TC_ACT_SHOT;
	return valid_client(&ip6->saddr, &ip6->daddr) ? TC_ACT_OK : TC_ACT_SHOT;
}

SEC("tc/mesh")
int gateway_mesh_ingress(struct __sk_buff *skb)
{
	void *data = (void *)(long)skb->data;
	void *end = (void *)(long)skb->data_end;
	struct ethhdr *eth = data;
	struct ipv6hdr *ip6 = (void *)(eth + 1);

	if ((void *)(eth + 1) > end || eth->h_proto != bpf_htons(ETH_P_IPV6))
		return TC_ACT_OK;
	if ((void *)(ip6 + 1) > end)
		return TC_ACT_SHOT;
	if (bpf_ntohs(ip6->daddr.s6_addr16[0]) == 0xfdac)
		return valid_client(&ip6->daddr, &ip6->saddr) ? TC_ACT_OK : TC_ACT_SHOT;
	if (bpf_ntohl(ip6->daddr.s6_addr32[0]) == MESH_WORD0 &&
	    bpf_ntohl(ip6->daddr.s6_addr32[1]) == MESH_WORD1 &&
	    bpf_ntohl(ip6->daddr.s6_addr32[2]) == MESH_WORD2 &&
	    bpf_ntohl(ip6->daddr.s6_addr32[3]) == MESH_WORD3)
		return TC_ACT_OK;
	return (bpf_ntohs(ip6->daddr.s6_addr16[0]) & 0xffc0) == 0xfe80 ||
	       ip6->daddr.s6_addr[0] == 0xff ? TC_ACT_OK : TC_ACT_SHOT;
}

char LICENSE[] SEC("license") = "GPL";
