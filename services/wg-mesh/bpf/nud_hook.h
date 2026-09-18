/* SPDX-License-Identifier: AGPL-3.0 */
/* Atlas WG Mesh NUD failure tracking: NUD_REACHABLE resets the failure
 * count, NUD_FAILED increments it, and 20 consecutive failures remove the
 * VM from remote_vms, clear the Linux neighbour entry, and remove the
 * failure counter.
 */
#ifndef ATLAS_NUD_HOOK_H
#define ATLAS_NUD_HOOK_H

#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ipv6.h>
#include <linux/neighbour.h>

#include "state.h"

#define ATLAS_NUD_FAILURE_LIMIT 20

#ifndef AF_INET6
#define AF_INET6 10
#endif

/* Raw tracepoint context for tracepoint/neigh/neigh_timer_handler. Layout
 * matches /sys/kernel/tracing/events/neigh/neigh_timer_handler/format.
 */
struct trace_event_raw_neigh_timer_handler
{
	__u16 common_type;
	__u8 common_flags;
	__u8 common_preempt_count;
	__s32 common_pid;

	__u32 family;
	__u32 dev;

	__u8 lladdr[32];
	__u8 lladdr_len;

	__u8 flags;
	__u8 nud_state;
	__u8 type;
	__u8 dead;

	__s32 refcnt;

	__u8 primary_key4[4];
	__u8 primary_key6[16];

	unsigned long confirmed;
	unsigned long updated;
	unsigned long used;

	__u32 err;
};

/* Raw tracepoint context for tracepoint/neigh/neigh_update. Layout
 * matches /sys/kernel/tracing/events/neigh/neigh_update/format.
 */
struct trace_event_raw_neigh_update
{
	__u16 common_type;
	__u8 common_flags;
	__u8 common_preempt_count;
	__s32 common_pid;

	__u32 family;
	__u32 dev;

	__u8 lladdr[32];
	__u8 lladdr_len;

	__u8 flags;
	__u8 nud_state;
	__u8 type;
	__u8 dead;

	__s32 refcnt;

	__u8 primary_key4[4];
	__u8 primary_key6[16];

	unsigned long confirmed;
	unsigned long updated;
	unsigned long used;

	__u8 new_lladdr[32];
	__u8 new_state;

	__u32 update_flags;
	__u32 pid;
};

/* Kernel kfunc provided by the Atlas neighbour module. */
extern int atlas_delete_neigh(__u32 ifindex, __u64 addr_hi, __u64 addr_lo) __ksym;

/* Get the IPv6 neighbour address from the tracepoint. */
static __always_inline void get_nud_address(const __u8 *primary_key6, struct in6_addr *address)
{
	__builtin_memcpy(address, primary_key6, sizeof(*address));
}

/* Convert the IPv6 address into the scalar arguments expected by
 * atlas_delete_neigh().
 */
static __always_inline void split_address(const struct in6_addr *address, __u64 *addr_hi, __u64 *addr_lo)
{
	__builtin_memcpy(addr_hi, &address->s6_addr[0], sizeof(*addr_hi));

	__builtin_memcpy(addr_lo, &address->s6_addr[8], sizeof(*addr_lo));
}

/* Reset the consecutive failure counter. Keep the entry with value 0
 * rather than deleting it, so the state explicitly represents a
 * successful neighbour.
 */
static __always_inline void reset_nud_failures(const struct in6_addr *vm)
{
	__u32 zero = 0;

	bpf_map_update_elem(&nud_failures, vm, &zero, BPF_ANY);
}

/* Increment the consecutive failure counter. Returns the new count. */
static __always_inline __u32 increment_nud_failures(const struct in6_addr *vm)
{
	__u32 *count;
	__u32 new_count;

	count = bpf_map_lookup_elem(&nud_failures, vm);

	if (!count)
	{
		new_count = 1;

		bpf_map_update_elem(&nud_failures, vm, &new_count, BPF_ANY);

		return new_count;
	}

	new_count = *count + 1;

	bpf_map_update_elem(&nud_failures, vm, &new_count, BPF_ANY);

	return new_count;
}

/* Remove a failed remote VM. */
static __always_inline void remove_remote_vm(const struct in6_addr *vm)
{
	struct config *local_config;
	__u64 addr_hi;
	__u64 addr_lo;

	local_config = get_config();

	if (!local_config) return;

	bpf_map_delete_elem(&remote_vms, vm);

	split_address(vm, &addr_hi, &addr_lo);

		/* Clear the corresponding Linux neighbour entry. */
	atlas_delete_neigh(local_config->discovery_ifindex, addr_hi, addr_lo);

	bpf_map_delete_elem(&nud_failures, vm);
}

/* NUD failure hook. neigh_timer_handler observes the NUD timer processing
 * where the neighbour reaches NUD_FAILED.
 */
SEC("tracepoint/neigh/neigh_timer_handler")
int handle_atlas_nud_failure(struct trace_event_raw_neigh_timer_handler *ctx)
{
	struct in6_addr vm = {};
	__u32 count;

	if (ctx->family != AF_INET6) return 0;

	get_nud_address(ctx->primary_key6, &vm);

		/* Only track addresses that Atlas currently knows as remote VMs. */
	if (!bpf_map_lookup_elem(&remote_vms, &vm)) return 0;

		/* The tracepoint exposes the neighbour flags as a u8. NTF_EXT_LEARNED
	 * identifies the externally learned Atlas neighbour.
	 */
	if (!(ctx->flags & NTF_EXT_LEARNED)) return 0;

	if (ctx->nud_state != NUD_FAILED) return 0;

	count = increment_nud_failures(&vm);

	if (count < ATLAS_NUD_FAILURE_LIMIT) return 0;

		/* 20 consecutive failures: remove the remote_vms entry, the Linux
	 * neighbour entry, and the NUD failure counter.
	 */
	remove_remote_vm(&vm);

	return 0;
}

/* NUD success hook. neigh_update exposes the state that the update is
 * changing to. The current state may still be the old state, so use
 * new_state rather than nud_state.
 */
SEC("tracepoint/neigh/neigh_update")
int handle_atlas_nud_reachable(struct trace_event_raw_neigh_update *ctx)
{
	struct in6_addr vm = {};

	if (ctx->family != AF_INET6) return 0;

	get_nud_address(ctx->primary_key6, &vm);

		/* Only reset counters for addresses that Atlas currently knows as
	 * remote VMs.
	 */
	if (!bpf_map_lookup_elem(&remote_vms, &vm)) return 0;

		/* The tracepoint exposes the neighbour flags as a u8. NTF_EXT_LEARNED
	 * identifies the externally learned Atlas neighbour.
	 */
	if (!(ctx->flags & NTF_EXT_LEARNED)) return 0;

		/* Reset the failure streak only when the neighbour is being changed to
	 * NUD_REACHABLE: new_state is the requested state, nud_state the current
	 * one.
	 */
	if (ctx->new_state != NUD_REACHABLE) return 0;

	reset_nud_failures(&vm);

	return 0;
}

#endif /* ATLAS_NUD_HOOK_H */
