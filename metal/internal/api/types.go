package api

import (
	"time"

	"github.com/frappe/atlas/metal/internal/vm"
)

type createRequest struct {
	VCPUs   int      `json:"vcpus"`
	MemMiB  int      `json:"mem_mib"`
	DiskMiB int      `json:"disk_mib"`
	Image   string   `json:"image"`
	Network string   `json:"network"`
	SSHKeys []string `json:"ssh_keys"`
}

func (r createRequest) spec() vm.Spec {
	return vm.Spec{
		VCPUs:   r.VCPUs,
		MemMiB:  r.MemMiB,
		DiskMiB: r.DiskMiB,
		Image:   vm.ImageRef{Name: r.Image},
		Network: vm.NetworkRef{Name: r.Network},
		SSHKeys: r.SSHKeys,
	}
}

type stopRequest struct {
	Force bool `json:"force"`
}

// resizeRequest uses pointers so an omitted field differs from a zero value. Only
// disk_mib is supported today. vcpus and mem_mib are rejected.
type resizeRequest struct {
	VCPUs   *int `json:"vcpus"`
	MemMiB  *int `json:"mem_mib"`
	DiskMiB *int `json:"disk_mib"`
}

type virtualMachineResponse struct {
	ID      string       `json:"id"`
	State   string       `json:"state"`
	VCPUs   int          `json:"vcpus"`
	MemMiB  int          `json:"mem_mib"`
	Image   string       `json:"image"`
	Network string       `json:"network"`
	IP      string       `json:"ip"`
	MAC     string       `json:"mac"`
	PID     int          `json:"pid"`
	Disk    diskResponse `json:"disk"`
}

// The generated application programming interface specification carries their
// shape.

type virtualMachineListResponse struct {
	VMs []virtualMachineResponse `json:"vms"`
}

type snapshotListResponse struct {
	Snapshots []snapshotResponse `json:"snapshots"`
}

type imageListResponse struct {
	Images []imageResponse `json:"images"`
}

type snapshotCreatedResponse struct {
	Name   string `json:"name"`
	Memory bool   `json:"memory"`
}

type imageCreatedResponse struct {
	Ref string `json:"ref"`
}

type errorBody struct {
	Message string `json:"message"`
}

type errorResponse struct {
	Error errorBody `json:"error"`
}

type diskResponse struct {
	SizeMiB   int `json:"size_mib"`
	UsedMiB   int `json:"used_mib"`
	Snapshots int `json:"snapshots"`
}

func toVirtualMachine(i vm.Info) virtualMachineResponse {
	return virtualMachineResponse{
		ID:      i.ID,
		State:   string(i.State),
		VCPUs:   i.VCPUs,
		MemMiB:  i.MemMiB,
		Image:   i.Image,
		Network: i.Network,
		IP:      i.IP,
		MAC:     i.MAC,
		PID:     i.PID,
		Disk:    diskResponse{SizeMiB: i.DiskMiB, UsedMiB: i.DiskUsedMiB, Snapshots: i.Snapshots},
	}
}

type snapshotRequest struct {
	Name   string `json:"name"`
	Memory bool   `json:"memory"`
}

type promoteRequest struct {
	Image string `json:"image"`
}

type snapshotResponse struct {
	Name      string `json:"name"`
	VMID      string `json:"vm_id"`
	Memory    bool   `json:"memory"`
	SizeMiB   int    `json:"size_mib"`
	UsedMiB   int    `json:"used_mib"`
	CreatedAt string `json:"created_at"`
}

func toSnapshot(vmID string, s vm.Snapshot) snapshotResponse {
	return snapshotResponse{
		Name: s.Name, VMID: vmID, Memory: s.Memory,
		SizeMiB: s.SizeMiB, UsedMiB: s.UsedMiB, CreatedAt: rfc3339(s.CreatedAt),
	}
}

type imageResponse struct {
	Ref       string `json:"ref"`
	Warm      bool   `json:"warm"`
	SizeMiB   int    `json:"size_mib"`
	CreatedAt string `json:"created_at"`
}

func toImage(i vm.Image) imageResponse {
	return imageResponse{Ref: i.Ref, Warm: i.Warm, SizeMiB: i.SizeMiB, CreatedAt: rfc3339(i.CreatedAt)}
}

// rfc3339 formats a time for the application programming interface and returns an empty string for the zero time.
func rfc3339(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	return t.Format(time.RFC3339)
}
