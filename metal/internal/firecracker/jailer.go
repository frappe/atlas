package firecracker

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// apiSockRel is the firecracker API socket path relative to the chroot root.
const apiSockRel = "run/firecracker.socket"

// chrootRoot is where jailer builds the VM's chroot: <base>/<exec>/<id>/root.
func (c Config) chrootRoot(id string) string {
	return filepath.Join(c.ChrootBase, filepath.Base(c.FirecrackerBin), id, "root")
}

// sockPath is the host path to a VM's API socket.
func (c Config) sockPath(id string) string {
	return filepath.Join(c.chrootRoot(id), apiSockRel)
}

// jailerArgs builds the argv the systemd unit runs: jailer flags, then "--",
// then the firecracker flags that run inside the chroot.
func (c Config) jailerArgs(id string, uid, gid uint32, netns string) []string {
	return []string{
		"--id", id,
		"--exec-file", c.FirecrackerBin,
		"--uid", fmt.Sprint(uid),
		"--gid", fmt.Sprint(gid),
		"--chroot-base-dir", c.ChrootBase,
		"--netns", netns,
		"--",
		"--api-sock", apiSockRel,
	}
}

// writeJailerEnv writes the EnvironmentFile the metal-vm@ template reads. systemd
// word-splits $JAILER_ARGS in ExecStart, so the args must not contain spaces.
func (c Config) writeJailerEnv(id string, args []string) error {
	if err := os.MkdirAll(c.vmDir(id), 0o750); err != nil {
		return err
	}
	line := "JAILER_ARGS=" + strings.Join(args, " ") + "\n"
	return os.WriteFile(filepath.Join(c.vmDir(id), "jailer.env"), []byte(line), 0o640)
}
