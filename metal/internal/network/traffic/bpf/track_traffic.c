/* SPDX-License-Identifier: AGPL-3.0 */

#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>

#define ETHERNET_DESTINATION_OFFSET 0
#define ETHERNET_GROUP_ADDRESS_BIT 0x01
#define ETHERNET_TYPE_OFFSET 12
#define ETHERNET_TYPE_IPV4 0x0800
#define ETHERNET_TYPE_IPV6 0x86DD

/*
 * Stores the last time meaningful network traffic was seen for the VM.
 * Timestamp is from bpf_ktime_get_ns().
 */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u64);
} activity_by_user_id SEC(".maps");

/*
 * One-shot traffic watch flag.
 * Userspace sets the value to 1 when it wants to be notified about
 * the next qualifying packet.
 */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u32);
} watch_by_user_id SEC(".maps");

struct traffic_counters {
	__u64 bytes;
	__u64 packets;
};

/* Egress on the TAP: host stack writing toward the guest. */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct traffic_counters);
} rx_counters_by_user_id SEC(".maps");

/* Ingress on the TAP: the guest writing toward the host stack. */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct traffic_counters);
} tx_counters_by_user_id SEC(".maps");

/* Sends the VM user ID to userspace when watched traffic is detected. */
struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 1 << 16);
} traffic_events SEC(".maps");

/* Set by the loader for the VM this program is attached to. */
const volatile __u32 virtual_machine_user_id = 0;

static __always_inline void add_traffic_counters(void *counters_map, __u32 user_id, __u32 packet_length)
{
	struct traffic_counters *counters = bpf_map_lookup_elem(counters_map, &user_id);
	if (!counters) {
		/* Another CPU can create the entry before this insertion. Never replace it. */
		struct traffic_counters initial = {};
		bpf_map_update_elem(counters_map, &user_id, &initial, BPF_NOEXIST);
		counters = bpf_map_lookup_elem(counters_map, &user_id);
		if (!counters)
			return;
	}

	__sync_fetch_and_add(&counters->bytes, packet_length);
	__sync_fetch_and_add(&counters->packets, 1);
}

/* Only IPv4 and IPv6 packets are considered activity. */
static __always_inline int is_ip_packet(struct __sk_buff *packet)
{
	__u16 ethernet_type;

	if (bpf_skb_load_bytes(
		    packet,
		    ETHERNET_TYPE_OFFSET,
		    &ethernet_type,
		    sizeof(ethernet_type)) < 0)
		return 0;

	return ethernet_type == __builtin_bswap16(ETHERNET_TYPE_IPV4) ||
	       ethernet_type == __builtin_bswap16(ETHERNET_TYPE_IPV6);
}

/*
 * Ignore multicast/broadcast traffic.
 *
 * The host can generate IPv6 multicast listener traffic on the TAP device.
 * That is host control traffic and should not keep an idle VM awake.
 */
static __always_inline int is_unicast_packet(struct __sk_buff *packet)
{
	__u8 first_destination_byte;

	if (bpf_skb_load_bytes(
		    packet,
		    ETHERNET_DESTINATION_OFFSET,
		    &first_destination_byte,
		    sizeof(first_destination_byte)) < 0)
		return 0;

	return (first_destination_byte & ETHERNET_GROUP_ADDRESS_BIT) == 0;
}

SEC("tc")
int track_traffic(struct __sk_buff *packet)
{
	if (!is_ip_packet(packet) || !is_unicast_packet(packet))
		return TCX_NEXT;

	__u32 user_id = virtual_machine_user_id;
	__u64 packet_time = bpf_ktime_get_ns();

	/* Always update the VM's last activity timestamp. */
	bpf_map_update_elem(
		&activity_by_user_id,
		&user_id,
		&packet_time,
		BPF_ANY
	);

	add_traffic_counters(&rx_counters_by_user_id, user_id, packet->len);

	/*
	 * Only notify userspace if it explicitly armed a watch for this VM.
	 */
	__u32 *watch = bpf_map_lookup_elem(&watch_by_user_id, &user_id);
	if (!watch || *watch == 0)
		return TCX_NEXT;

	/*
	 * Reserve the event before clearing the watch.
	 * If the ring buffer is full, the watch remains armed for the next packet.
	 */
	__u32 *event = bpf_ringbuf_reserve(
		&traffic_events,
		sizeof(*event),
		0
	);
	if (!event)
		return TCX_NEXT;

	/* One-shot notification: userspace must re-arm it if needed. */
	*watch = 0;
	*event = user_id;
	bpf_ringbuf_submit(event, 0);

	return TCX_NEXT;
}

/* Counts only; activity and watch bookkeeping stays on the egress side. */
SEC("tc")
int track_traffic_ingress(struct __sk_buff *packet)
{
	if (!is_ip_packet(packet) || !is_unicast_packet(packet))
		return TCX_NEXT;

	add_traffic_counters(&tx_counters_by_user_id, virtual_machine_user_id, packet->len);

	return TCX_NEXT;
}

char _license[] SEC("license") = "GPL";
