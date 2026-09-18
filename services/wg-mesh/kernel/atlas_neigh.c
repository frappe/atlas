#include <linux/bpf.h>
#include <linux/errno.h>
#include <linux/if_ether.h>
#include <linux/module.h>
#include <linux/netdevice.h>
#include <linux/neighbour.h>

#include <net/neighbour.h>
#include <net/ndisc.h>

#include <linux/btf.h>
#include <linux/btf_ids.h>

__bpf_kfunc int atlas_register_neigh(
	__u32 ifindex,
	__u64 addr_hi,
	__u64 addr_lo,
	__u64 mac)
{
	struct net_device *device;
	struct neighbour *neighbour;
	struct in6_addr address;
	unsigned char lladdr[ETH_ALEN];
	int result;

	__builtin_memset(&address, 0, sizeof(address));

	__builtin_memcpy(
		&address.s6_addr32[0],
		&addr_hi,
		sizeof(addr_hi));

	__builtin_memcpy(
		&address.s6_addr32[2],
		&addr_lo,
		sizeof(addr_lo));

	__builtin_memset(lladdr, 0, sizeof(lladdr));

	__builtin_memcpy(
		lladdr,
		&mac,
		ETH_ALEN);

	device = dev_get_by_index(&init_net, ifindex);
	if (!device)
		return -ENODEV;

	pr_info(
		"atlas_neigh: ifindex=%u dev=%s addr=%pI6 "
		"mac=%02x:%02x:%02x:%02x:%02x:%02x\n",
		ifindex,
		device->name,
		&address,
		lladdr[0],
		lladdr[1],
		lladdr[2],
		lladdr[3],
		lladdr[4],
		lladdr[5]);

	neighbour = neigh_lookup(
		&nd_tbl,
		&address,
		device);

	if (!neighbour) {
		neighbour = __neigh_create(
			&nd_tbl,
			&address,
			device,
			true);

		if (IS_ERR(neighbour)) {
			result = PTR_ERR(neighbour);

			pr_info(
				"atlas_neigh: __neigh_create failed: %d\n",
				result);

			dev_put(device);
			return result;
		}

		pr_info("atlas_neigh: created neighbour\n");
	} else {
		pr_info(
			"atlas_neigh: found neighbour state=0x%x flags=0x%x\n",
			neighbour->nud_state,
			neighbour->flags);
	}

	/*
	 * Update the MAC and NUD state.
	 *
	 * IMPORTANT:
	 * Do not use NEIGH_UPDATE_F_ADMIN here.
	 * ADMIN would make neigh_update_flags() treat the
	 * omitted MANAGED flag as "clear MANAGED".
	 */
	result = neigh_update(
		neighbour,
		lladdr,
		NUD_REACHABLE,
		NEIGH_UPDATE_F_OVERRIDE,
		0);

	if (result) {
		pr_info(
			"atlas_neigh: lladdr update failed: %d\n",
			result);

		neigh_release(neighbour);
		dev_put(device);
		return result;
	}

	pr_info(
		"atlas_neigh: lladdr update state=0x%x flags=0x%x\n",
		neighbour->nud_state,
		neighbour->flags);

	/*
	 * Set the persistent neighbour flags.
	 *
	 * This sets:
	 *   NTF_EXT_LEARNED
	 *   NTF_MANAGED
	 */
	result = neigh_update(
		neighbour,
		NULL,
		NUD_REACHABLE,
		NEIGH_UPDATE_F_ADMIN |
		NEIGH_UPDATE_F_EXT_LEARNED |
		NEIGH_UPDATE_F_MANAGED,
		0);

	pr_info(
		"atlas_neigh: managed update result=%d state=0x%x flags=0x%x\n",
		result,
		neighbour->nud_state,
		neighbour->flags);

	neigh_release(neighbour);
	dev_put(device);

	return result;
}

__bpf_kfunc int atlas_delete_neigh(
	__u32 ifindex,
	__u64 addr_hi,
	__u64 addr_lo)
{
	struct net_device *device;
	struct neighbour *neighbour;
	struct in6_addr address;
	int result;

	__builtin_memset(&address, 0, sizeof(address));

	__builtin_memcpy(
		&address.s6_addr32[0],
		&addr_hi,
		sizeof(addr_hi));

	__builtin_memcpy(
		&address.s6_addr32[2],
		&addr_lo,
		sizeof(addr_lo));

	device = dev_get_by_index(&init_net, ifindex);
	if (!device)
		return -ENODEV;

	pr_info(
		"atlas_neigh: delete request "
		"ifindex=%u dev=%s addr=%pI6\n",
		ifindex,
		device->name,
		&address);

	neighbour = neigh_lookup(
		&nd_tbl,
		&address,
		device);

	if (!neighbour) {
		pr_info(
			"atlas_neigh: delete neighbour not found: "
			"ifindex=%u addr=%pI6\n",
			ifindex,
			&address);

		dev_put(device);
		return -ENOENT;
	}

	pr_info(
		"atlas_neigh: delete neighbour found "
		"state=0x%x flags=0x%x refcnt=%d\n",
		neighbour->nud_state,
		neighbour->flags,
		refcount_read(&neighbour->refcnt));

	/*
	 * Only Atlas-managed externally-learned neighbours
	 * may be removed.
	 */
	if (!(neighbour->flags & NTF_EXT_LEARNED) ||
	    !(neighbour->flags & NTF_MANAGED)) {
		pr_info(
			"atlas_neigh: refusing to delete "
			"non-Atlas neighbour\n");

		neigh_release(neighbour);
		dev_put(device);
		return -EPERM;
	}

	/*
	 * Clear the managed and externally-learned flags and mark the
	 * entry incomplete, exactly as the RTM_DELNEIGH path does. The
	 * ADMIN update clears every omitted flag, and the neighbour
	 * garbage collector then removes the entry. The kernel exports
	 * no function that removes a neighbour from a module directly.
	 */
	result = neigh_update(
		neighbour,
		NULL,
		NUD_INCOMPLETE,
		NEIGH_UPDATE_F_ADMIN,
		0);

	if (result) {
		pr_info(
			"atlas_neigh: delete update failed: %d\n",
			result);

		neigh_release(neighbour);
		dev_put(device);
		return result;
	}

	pr_info(
		"atlas_neigh: neighbour delete issued; "
		"the garbage collector removes the entry: "
		"ifindex=%u addr=%pI6\n",
		ifindex,
		&address);

	/*
	 * neigh_update() performed the entry cleanup.
	 */
	neigh_release(neighbour);
	dev_put(device);

	return 0;
}

BTF_SET8_START(atlas_neigh_kfunc_ids)

BTF_ID_FLAGS(func, atlas_register_neigh)
BTF_ID_FLAGS(func, atlas_delete_neigh)

BTF_SET8_END(atlas_neigh_kfunc_ids)

static const struct btf_kfunc_id_set atlas_neigh_kfunc_set = {
	.owner = THIS_MODULE,
	.set = &atlas_neigh_kfunc_ids,
};

static int __init atlas_neigh_init(void)
{
	int result;

	/*
	 * Register under the common hook, which serves every program type.
	 * The NDP programs are tc and the NUD program is a tracepoint, and
	 * the tracepoint type has no hook of its own.
	 */
	result = register_btf_kfunc_id_set(
		BPF_PROG_TYPE_UNSPEC,
		&atlas_neigh_kfunc_set);

	if (result) {
		pr_err(
			"atlas_neigh: kfunc registration failed: %d\n",
			result);

		return result;
	}

	pr_info(
		"atlas_neigh: register and delete kfuncs registered "
		"for every program type\n");

	return 0;
}

static void __exit atlas_neigh_exit(void)
{
	pr_info("atlas_neigh: unloaded\n");
}

module_init(atlas_neigh_init);
module_exit(atlas_neigh_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Atlas");
MODULE_DESCRIPTION(
	"Atlas WG Mesh neighbour registration and deletion kfuncs");