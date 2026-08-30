/* SPDX-License-Identifier: AGPL-3.0 */
/*
 * Atlas WG Mesh BPF object assembly file.
 *
 * BPF maps and all three TC hooks are deliberately compiled as one object.
 * That lets the loader pin one shared set of maps for every hook.
 */

#include "debug.h"
#include "state.h"
#include "vm_hook.h"
#include "uplink_hook.h"
#include "wireguard_hook.h"

/* The kernel BPF ABI requires this string for GPL-only helper access. */
char LICENSE[] SEC("license") = "GPL";
