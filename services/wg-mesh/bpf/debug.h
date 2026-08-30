/* SPDX-License-Identifier: AGPL-3.0 */
#ifndef ATLAS_DEBUG_H
#define ATLAS_DEBUG_H

#include "protocol.h"

enum debug_hook
{
	DEBUG_VM,
	DEBUG_UPLINK,
	DEBUG_WIREGUARD
};
enum debug_verdict
{
	DEBUG_ACCEPT,
	DEBUG_DROP,
	DEBUG_REDIRECT
};
enum debug_direction
{
	DEBUG_NO_DIRECTION,
	DEBUG_SEND,
	DEBUG_RECEIVE
};

struct debug_config
{
	__u8 enabled;
};

struct debug_stats
{
	__u64 accepted;
	__u64 dropped;
	__u64 protocol_sent;
	__u64 protocol_received;
	__u64 lost;
};

struct debug_event
{
	__u64 timestamp;
	/* Packet events use source/destination;
	protocol events use vm/host. */
	struct in6_addr source;
	struct in6_addr destination;
	struct in6_addr vm;
	struct in6_addr host;
	__u8 tenant[4];
	__u8 hook;
	__u8 verdict;
	__u8 operation;
	__u8 direction;
};

struct
{
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__type(key, __u32);
	__type(value, struct debug_config);
	__uint(max_entries, 1);
} debug_config SEC(".maps");

struct
{
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__type(key, __u32);
	__type(value, struct debug_stats);
	__uint(max_entries, 1);
} debug_stats SEC(".maps");

/* Global ring buffer; it is allocated even while debug is disabled. */
struct
{
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 4194304);
} debug_events SEC(".maps");

static __always_inline void record_debug_stats(__u8 packet_action,
											   __u8 protocol_direction)
{
	__u32 key = 0;
	struct debug_stats *stats = bpf_map_lookup_elem(&debug_stats, &key);

	if (!stats)
		return;

	if (packet_action == DEBUG_DROP)
		stats->dropped++;
	else
		stats->accepted++;

	if (protocol_direction == DEBUG_SEND)
		stats->protocol_sent++;
	else if (protocol_direction == DEBUG_RECEIVE)
		stats->protocol_received++;
}

static __always_inline int is_debug_enabled(void)
{
	__u32 key = 0;
	struct debug_config *settings = bpf_map_lookup_elem(&debug_config, &key);

	return settings && settings->enabled;
}

static __always_inline void record_debug_event_loss(void)
{
	__u32 key = 0;
	struct debug_stats *stats = bpf_map_lookup_elem(&debug_stats, &key);

	if (stats)
		stats->lost++;
}

static __always_inline void emit_packet_debug_event(
	__u8 hook, __u8 packet_action, const struct in6_addr *source_address,
	const struct in6_addr *destination_address)
{
	struct debug_event *event;

	if (!is_debug_enabled())
		return;

	record_debug_stats(packet_action, DEBUG_NO_DIRECTION);
	event = bpf_ringbuf_reserve(&debug_events, sizeof(*event), 0);
	if (!event)
	{
		/* Events are best-effort; forwarding must not wait for a reader. */
		record_debug_event_loss();
		return;
	}

	__builtin_memset(event, 0, sizeof(*event));
	event->timestamp = bpf_ktime_get_ns();
	event->hook = hook;
	event->verdict = packet_action;
	event->source = *source_address;
	event->destination = *destination_address;
	__builtin_memcpy(event->tenant, &source_address->s6_addr[4], 4);
	bpf_ringbuf_submit(event, 0);
}

static __always_inline void emit_protocol_debug_event(
	__u8 hook, __u8 protocol_direction, __u8 message_operation,
	const struct in6_addr *virtual_machine,
	const struct in6_addr *virtual_machine_host)
{
	struct debug_event *event;

	if (!is_debug_enabled())
		return;

	record_debug_stats(DEBUG_ACCEPT, protocol_direction);
	event = bpf_ringbuf_reserve(&debug_events, sizeof(*event), 0);
	if (!event)
	{
		/* Keep the loss count visible for protocol-only troubleshooting. */
		record_debug_event_loss();
		return;
	}

	__builtin_memset(event, 0, sizeof(*event));
	event->timestamp = bpf_ktime_get_ns();
	event->hook = hook;
	event->verdict = DEBUG_ACCEPT;
	event->operation = message_operation;
	event->direction = protocol_direction;
	event->vm = *virtual_machine;

	if (virtual_machine_host)
		event->host = *virtual_machine_host;

	__builtin_memcpy(event->tenant, &virtual_machine->s6_addr[4], 4);
	bpf_ringbuf_submit(event, 0);
}

#endif /* ATLAS_DEBUG_H */
