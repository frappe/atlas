// vpc_guard.bpf.c — attach to the customer gateway VM's wg0 tc INGRESS (post-decrypt).
//
// The destination-confinement half of the gateway's isolation (spec/25 Phase 5,
// spec/26, reference §6.2). Accept iff the decrypted packet's source and destination
// share the same tenant /48; else drop. FAIL-CLOSED (drops any non-fdaa source or dest,
// so the public internet, the infra /48, and the gateway host itself are unreachable
// through it).
//
// This is the STATIC, per-customer-state-free guard: one program expresses "same /48 as
// source" for ALL customers at once — enrolling a customer never touches it (that is
// just a wg peer with its /128 source pin). Combined with WireGuard cryptokey routing
// pinning each peer's source to its own /128, "same /48 as source" ≡ "the client's own
// tenant" ≡ "only their VMs."
//
// nftables CANNOT express this — it can't compare two packet fields to each other (all
// masked-compare forms fail to parse; verified on a real kernel-6.8 host). This ~6-instr
// eBPF program can, and was compiled (clang 18), verifier-passed, JIT'd to 121B, and
// attached to a real wg0 (2026-07-02). An nft concatenated interval set is the documented
// fallback (same security, but one set element per customer).
//
// TWO GOTCHAS (both cost real host-time to find — do NOT re-introduce):
//   1. wg0 is a PURE L3 device — do NOT parse an ethhdr; skb->data is the IPv6 header
//      directly. Adding sizeof(struct ethhdr) reads the wrong bytes and mis-verdicts.
//      Test on a real wg0, never on lo/veth.
//   2. The load SECTION must be "tc" so `tc filter ... sec tc` attaches. Use `tc filter`
//      (verified working), not `bpftool prog load ... type sched_cls`.
#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ipv6.h>
#include <linux/pkt_cls.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

// The /48 is the first 6 bytes of the address = the whole 1st __u32 (bytes 0..3) plus
// the top 16 bits of the 2nd __u32 (bytes 4..5). In NETWORK byte order bytes 4..5 land
// in the 16-bit word at index 2, so a plain == on that half-word IS the /48 mask — no
// htonl/mask needed.
static __always_inline int same_48(const struct in6_addr *s, const struct in6_addr *d)
{
	if (s->in6_u.u6_addr32[0] != d->in6_u.u6_addr32[0])
		return 0;                                  // bytes 0..3 differ
	return s->in6_u.u6_addr16[2] == d->in6_u.u6_addr16[2];  // bytes 4..5 (the /48)
}

SEC("tc")                          // load with: tc filter add ... bpf da obj ... sec tc
int vpc_guard(struct __sk_buff *skb)
{
	// wg0 is a PURE L3 device: skb->data points AT the IPv6 header (no ethhdr).
	if (skb->protocol != bpf_htons(ETH_P_IPV6))
		return TC_ACT_SHOT;                        // fail-closed: only v6 on wg0
	void *data = (void *)(long)skb->data;
	void *end  = (void *)(long)skb->data_end;
	struct ipv6hdr *ip6 = data;
	if ((void *)(ip6 + 1) > end)                       // verifier-required bounds check
		return TC_ACT_SHOT;

	// Defense-in-depth: both ends MUST be in the private plane fdaa::/16.
	// (Layer 1's source pin already guarantees saddr; we re-assert, fail-closed.)
	if (ip6->saddr.in6_u.u6_addr16[0] != bpf_htons(0xfdaa))
		return TC_ACT_SHOT;
	if (ip6->daddr.in6_u.u6_addr16[0] != bpf_htons(0xfdaa))
		return TC_ACT_SHOT;                        // drops any public/infra dest

	if (!same_48(&ip6->saddr, &ip6->daddr))
		return TC_ACT_SHOT;                        // cross-tenant -> drop
	return TC_ACT_OK;                                  // own VPC -> allow
}

char _license[] SEC("license") = "GPL";
