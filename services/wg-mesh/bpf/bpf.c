/* SPDX-License-Identifier: AGPL-3.0 */
/* One object, so every hook shares one set of pinned maps. */

#include "host.h"
#include "uplink.h"
#include "wireguard.h"

/* GPL-only helpers need this string. */
char LICENSE[] SEC("license") = "GPL";
