/* SPDX-License-Identifier: AGPL-3.0 */

#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>

#define ETHERNET_DESTINATION_OFFSET 0
#define ETHERNET_GROUP_ADDRESS_BIT 0x01
#define ETHERNET_TYPE_OFFSET 12
#define ETHERNET_TYPE_IPV4 0x0800
#define ETHERNET_TYPE_IPV6 0x86DD
#define IP_HEADER_OFFSET 14
#define IPV4_FRAGMENT_OFFSET 20
#define IPV4_PROTOCOL_OFFSET 23
#define IPV4_SOURCE_OFFSET 26
#define IPV4_DESTINATION_OFFSET 30
#define IPV6_NEXT_HEADER_OFFSET 20
#define IPV6_SOURCE_OFFSET 22
#define IPV6_DESTINATION_OFFSET 38
#define IPV6_TRANSPORT_OFFSET 54
#define IP_PROTOCOL_ICMP 1
#define IP_PROTOCOL_TCP 6
#define IP_PROTOCOL_UDP 17
#define IP_PROTOCOL_ICMPV6 58
#define TCP_FLAGS_OFFSET 13
#define TCP_FLAG_SYN 0x02
#define TCP_FLAG_RST 0x04
#define TCP_FLAG_ACK 0x10
#define FLOW_STARTED_BY_GUEST 1
#define FLOW_STARTED_BY_PEER 2

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

struct sent_counters {
	struct traffic_counters total;
	__u64 icmp_packets;
	__u64 udp_packets;
	__u64 tcp_syn_packets;
	__u64 tcp_rst_packets;
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
	__type(value, struct sent_counters);
} tx_counters_by_user_id SEC(".maps");

/* Sends the VM user ID to userspace when watched traffic is detected. */
struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 1 << 16);
} traffic_events SEC(".maps");

struct flow_key {
	__u32 user_id;
	__u8 guest_address[16];
	__u8 peer_address[16];
	__u16 guest_port;
	__u16 peer_port;
	__u8 protocol;
	__u8 ip_version;
	__u16 padding;
};

/*
 * Records who started each flow, so a reply to the guest is not a request.
 * A reply in a flow that this map loses counts as a request.
 */
struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 1 << 18);
	__type(key, struct flow_key);
	__type(value, __u8);
} flow_starters SEC(".maps");

/* Set by the loader for the VM this program is attached to. */
const volatile __u32 virtual_machine_user_id = 0;

static __always_inline struct traffic_counters *add_traffic_counters(void *counters_map, __u32 user_id, __u32 packet_length)
{
	struct traffic_counters *counters = bpf_map_lookup_elem(counters_map, &user_id);
	if (!counters) {
		/* Another CPU can create the entry before this insertion. Never replace it. */
		struct sent_counters initial = {};
		bpf_map_update_elem(counters_map, &user_id, &initial, BPF_NOEXIST);
		counters = bpf_map_lookup_elem(counters_map, &user_id);
		if (!counters)
			return 0;
	}

	__sync_fetch_and_add(&counters->bytes, packet_length);
	__sync_fetch_and_add(&counters->packets, 1);
	return counters;
}

struct ip_header {
	__u8 protocol;
	__u8 version;
	__u16 transport_offset;
};

/* Only the first fragment holds a transport header, so a later one fails. */
static __always_inline int read_ipv4_header(struct __sk_buff *packet, struct ip_header *header)
{
	__u8 first_header_byte;
	__u16 fragment_offset_and_flags;

	if (bpf_skb_load_bytes(packet, IP_HEADER_OFFSET, &first_header_byte, 1) < 0 ||
	    (first_header_byte >> 4) != 4 || (first_header_byte & 15) < 5 ||
	    bpf_skb_load_bytes(packet, IPV4_PROTOCOL_OFFSET, &header->protocol, 1) < 0)
		return 0;
	if (bpf_skb_load_bytes(packet, IPV4_FRAGMENT_OFFSET, &fragment_offset_and_flags, sizeof(fragment_offset_and_flags)) < 0 ||
	    (__builtin_bswap16(fragment_offset_and_flags) & 0x1fff) != 0)
		return 0;

	header->version = 4;
	header->transport_offset = IP_HEADER_OFFSET + (first_header_byte & 15) * 4;
	return 1;
}

static __always_inline int read_ipv6_header(struct __sk_buff *packet, struct ip_header *header)
{
	__u8 first_header_byte;

	if (bpf_skb_load_bytes(packet, IP_HEADER_OFFSET, &first_header_byte, 1) < 0 ||
	    (first_header_byte >> 4) != 6 ||
	    bpf_skb_load_bytes(packet, IPV6_NEXT_HEADER_OFFSET, &header->protocol, 1) < 0)
		return 0;

	header->version = 6;
	header->transport_offset = IPV6_TRANSPORT_OFFSET;
	return 1;
}

static __always_inline int read_ip_header(struct __sk_buff *packet, struct ip_header *header)
{
	__u16 ethernet_type;

	if (bpf_skb_load_bytes(packet, ETHERNET_TYPE_OFFSET, &ethernet_type, sizeof(ethernet_type)) < 0)
		return 0;
	if (ethernet_type == __builtin_bswap16(ETHERNET_TYPE_IPV4))
		return read_ipv4_header(packet, header);
	if (ethernet_type == __builtin_bswap16(ETHERNET_TYPE_IPV6))
		return read_ipv6_header(packet, header);
	return 0;
}

static __always_inline void count_sent_protocol(struct __sk_buff *packet, struct sent_counters *counters)
{
	struct ip_header header;

	if (!read_ip_header(packet, &header))
		return;

	if (header.protocol == IP_PROTOCOL_ICMP || header.protocol == IP_PROTOCOL_ICMPV6) {
		__sync_fetch_and_add(&counters->icmp_packets, 1);
		return;
	}
	if (header.protocol == IP_PROTOCOL_UDP) {
		__sync_fetch_and_add(&counters->udp_packets, 1);
		return;
	}
	if (header.protocol != IP_PROTOCOL_TCP)
		return;

	__u8 tcp_flags;
	if (bpf_skb_load_bytes(packet, header.transport_offset + TCP_FLAGS_OFFSET, &tcp_flags, 1) < 0)
		return;
	if (tcp_flags & TCP_FLAG_SYN) {
		__sync_fetch_and_add(&counters->tcp_syn_packets, 1);
	}
	if (tcp_flags & TCP_FLAG_RST)
		__sync_fetch_and_add(&counters->tcp_rst_packets, 1);
}

static __always_inline int read_flow_addresses(
	struct __sk_buff *packet,
	const struct ip_header *header,
	int is_sent_by_guest,
	struct flow_key *key)
{
	__u8 *source = is_sent_by_guest ? key->guest_address : key->peer_address;
	__u8 *destination = is_sent_by_guest ? key->peer_address : key->guest_address;

	if (header->version == 4)
		return bpf_skb_load_bytes(packet, IPV4_SOURCE_OFFSET, source, 4) >= 0 &&
		       bpf_skb_load_bytes(packet, IPV4_DESTINATION_OFFSET, destination, 4) >= 0;
	return bpf_skb_load_bytes(packet, IPV6_SOURCE_OFFSET, source, 16) >= 0 &&
	       bpf_skb_load_bytes(packet, IPV6_DESTINATION_OFFSET, destination, 16) >= 0;
}

static __always_inline int is_tcp_connection_start(struct __sk_buff *packet, const struct ip_header *header)
{
	__u8 tcp_flags;

	if (header->protocol != IP_PROTOCOL_TCP ||
	    bpf_skb_load_bytes(packet, header->transport_offset + TCP_FLAGS_OFFSET, &tcp_flags, 1) < 0)
		return 0;
	return (tcp_flags & (TCP_FLAG_SYN | TCP_FLAG_ACK)) == TCP_FLAG_SYN;
}

/* Fails for a packet with no ports. Such a packet counts as a request. */
static __always_inline int read_flow_key(
	struct __sk_buff *packet,
	int is_sent_by_guest,
	struct flow_key *key,
	int *is_connection_start)
{
	struct ip_header header;
	__u16 ports[2];

	if (!read_ip_header(packet, &header) ||
	    (header.protocol != IP_PROTOCOL_TCP && header.protocol != IP_PROTOCOL_UDP) ||
	    bpf_skb_load_bytes(packet, header.transport_offset, ports, sizeof(ports)) < 0 ||
	    !read_flow_addresses(packet, &header, is_sent_by_guest, key))
		return 0;

	int guest_port_index = is_sent_by_guest ? 0 : 1;

	key->user_id = virtual_machine_user_id;
	key->guest_port = ports[guest_port_index];
	key->peer_port = ports[1 - guest_port_index];
	key->protocol = header.protocol;
	key->ip_version = header.version;
	*is_connection_start = is_tcp_connection_start(packet, &header);
	return 1;
}

/* A UDP flow has no opening packet, so its first packet names the side that started it. */
static __always_inline void record_guest_flow(struct __sk_buff *packet)
{
	struct flow_key key = {};
	int is_connection_start;
	__u8 guest_started = FLOW_STARTED_BY_GUEST;

	if (!read_flow_key(packet, 1, &key, &is_connection_start))
		return;
	if (is_connection_start || (key.protocol == IP_PROTOCOL_UDP && !bpf_map_lookup_elem(&flow_starters, &key)))
		bpf_map_update_elem(&flow_starters, &key, &guest_started, BPF_ANY);
}

/* Also updates the map when a peer starts a flow. */
static __always_inline int is_reply_to_guest(struct __sk_buff *packet)
{
	struct flow_key key = {};
	int is_connection_start;
	__u8 peer_started = FLOW_STARTED_BY_PEER;

	if (!read_flow_key(packet, 0, &key, &is_connection_start))
		return 0;
	if (is_connection_start) {
		bpf_map_delete_elem(&flow_starters, &key);
		return 0;
	}

	__u8 *started_by = bpf_map_lookup_elem(&flow_starters, &key);
	if (started_by)
		return *started_by == FLOW_STARTED_BY_GUEST;
	if (key.protocol == IP_PROTOCOL_UDP)
		bpf_map_update_elem(&flow_starters, &key, &peer_started, BPF_ANY);
	return 0;
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

/*
 * ICMP and ICMPv6 carry pings, scans, and neighbor discovery, not guest work,
 * so they neither keep a VM awake nor wake it.
 */
static __always_inline int is_icmp_packet(struct __sk_buff *packet)
{
	__u16 ethernet_type;
	__u8 protocol;

	if (bpf_skb_load_bytes(packet, ETHERNET_TYPE_OFFSET, &ethernet_type, sizeof(ethernet_type)) < 0)
		return 0;
	if (ethernet_type == __builtin_bswap16(ETHERNET_TYPE_IPV4))
		return bpf_skb_load_bytes(packet, IPV4_PROTOCOL_OFFSET, &protocol, 1) == 0 && protocol == IP_PROTOCOL_ICMP;
	return bpf_skb_load_bytes(packet, IPV6_NEXT_HEADER_OFFSET, &protocol, 1) == 0 && protocol == IP_PROTOCOL_ICMPV6;
}

SEC("tc")
int track_guest_received(struct __sk_buff *packet)
{
	if (!is_ip_packet(packet) || !is_unicast_packet(packet))
		return TCX_NEXT;

	__u32 user_id = virtual_machine_user_id;
	add_traffic_counters(&rx_counters_by_user_id, user_id, packet->len);
	if (is_icmp_packet(packet) || is_reply_to_guest(packet))
		return TCX_NEXT;

	/* Update the VM's last activity timestamp. */
	__u64 packet_time = bpf_ktime_get_ns();
	bpf_map_update_elem(
		&activity_by_user_id,
		&user_id,
		&packet_time,
		BPF_ANY
	);

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

/* Counts and records flows; activity and watch bookkeeping stays on the egress side. */
SEC("tc")
int track_guest_sent(struct __sk_buff *packet)
{
	if (!is_ip_packet(packet) || !is_unicast_packet(packet))
		return TCX_NEXT;

	__u32 user_id = virtual_machine_user_id;
	struct traffic_counters *total = add_traffic_counters(&tx_counters_by_user_id, user_id, packet->len);
	if (total)
		count_sent_protocol(packet, (struct sent_counters *)total);
	record_guest_flow(packet);

	return TCX_NEXT;
}

char _license[] SEC("license") = "GPL";
