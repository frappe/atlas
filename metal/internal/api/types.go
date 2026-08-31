package api

import "github.com/frappe/atlas/metal/internal/vm"

type createReq struct {
	VCPUs   int      `json:"vcpus"`
	MemMiB  int      `json:"mem_mib"`
	DiskMiB int      `json:"disk_mib"`
	Image   string   `json:"image"`
	Network string   `json:"network"`
	SSHKeys []string `json:"ssh_keys"`
}

func (r createReq) spec() vm.Spec {
	return vm.Spec{
		VCPUs:   r.VCPUs,
		MemMiB:  r.MemMiB,
		DiskMiB: r.DiskMiB,
		Image:   vm.ImageRef{Name: r.Image},
		Network: vm.NetworkRef{Name: r.Network},
		SSHKeys: r.SSHKeys,
	}
}

type stopReq struct {
	Force bool `json:"force"`
}

// resizeReq uses pointers so an omitted field differs from a zero value. Only
// disk_mib is honored today; vcpus/mem_mib are rejected as not-implemented.
type resizeReq struct {
	VCPUs   *int `json:"vcpus"`
	MemMiB  *int `json:"mem_mib"`
	DiskMiB *int `json:"disk_mib"`
}

type vmResp struct {
	ID      string   `json:"id"`
	State   string   `json:"state"`
	VCPUs   int      `json:"vcpus"`
	MemMiB  int      `json:"mem_mib"`
	Image   string   `json:"image"`
	Network string   `json:"network"`
	IP      string   `json:"ip"`
	MAC     string   `json:"mac"`
	PID     int      `json:"pid"`
	Disk    diskResp `json:"disk"`
}

type diskResp struct {
	SizeMiB   int `json:"size_mib"`
	UsedMiB   int `json:"used_mib"`
	Snapshots int `json:"snapshots"`
}

func toVM(i vm.Info) vmResp {
	return vmResp{
		ID:      i.ID,
		State:   string(i.State),
		VCPUs:   i.VCPUs,
		MemMiB:  i.MemMiB,
		Image:   i.Image,
		Network: i.Network,
		IP:      i.IP,
		MAC:     i.MAC,
		PID:     i.PID,
		Disk:    diskResp{SizeMiB: i.DiskMiB, UsedMiB: i.DiskUsedMiB, Snapshots: i.Snapshots},
	}
}

type snapReq struct {
	Name string `json:"name"`
}

type snapResp struct {
	Name    string `json:"name"`
	VMID    string `json:"vm_id"`
	SizeMiB int    `json:"size_mib"`
	UsedMiB int    `json:"used_mib"`
}

func toSnap(vmID string, s vm.DiskSnapshot) snapResp {
	return snapResp{Name: s.Name, VMID: vmID, SizeMiB: s.SizeMiB, UsedMiB: s.UsedMiB}
}
