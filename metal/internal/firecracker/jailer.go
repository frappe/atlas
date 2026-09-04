package firecracker

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// apiSockRel is the firecracker API socket path relative to the chroot root.
const apiSockRel = "run/firecracker.socket"

// firecrackerLogRel is the Firecracker log path inside the chroot.
const firecrackerLogRel = "firecracker.log"

// chrootRoot returns the path where jailer builds the VM chroot. The base is the
// VM's own directory, so removing that directory takes the chroot with it, and
// the kernel hard link stays on one filesystem.
func (c Config) chrootRoot(id string) string {
	return filepath.Join(c.vmDir(id), filepath.Base(c.FirecrackerBin), id, "root")
}

// sockPath returns the short path metald dials for a VM's API socket.
func (c Config) sockPath(id string) string {
	return filepath.Join(c.SocketsDir, id+".sock")
}

// chrootSockPath returns the real socket path inside the VM jail.
func (c Config) chrootSockPath(id string) string {
	return filepath.Join(c.chrootRoot(id), apiSockRel)
}

// linkSocket creates the short socket symlink before Firecracker starts.
func (c Config) linkSocket(id string) error {
	if err := os.MkdirAll(c.SocketsDir, 0o700); err != nil {
		return err
	}
	link := c.sockPath(id)
	if err := os.Remove(link); err != nil && !os.IsNotExist(err) {
		return err
	}
	return os.Symlink(c.chrootSockPath(id), link)
}

// jailerArgs builds the argv the systemd unit runs: jailer flags, then "--",
// then the firecracker flags that run inside the chroot.
func (c Config) jailerArgs(id string, uid, gid uint32, netns string) []string {
	return []string{
		"--id", id,
		"--exec-file", c.FirecrackerBin,
		"--uid", fmt.Sprint(uid),
		"--gid", fmt.Sprint(gid),
		"--chroot-base-dir", c.vmDir(id),
		"--netns", netns,
		"--",
		"--api-sock", apiSockRel,
		"--log-path", firecrackerLogRel,
		"--level", "Warn",
	}
}

// writeJailerEnv writes the EnvironmentFile the metal-vm@ template reads. systemd
// word-splits $JAILER_ARGS in ExecStart, so the args must not contain spaces.
func (c Config) writeJailerEnv(id string, arguments []string) error {
	line := "JAILER_ARGS=" + strings.Join(arguments, " ") + "\n"
	return atomicWriteFile(filepath.Join(c.vmDir(id), "jailer.env"), []byte(line), 0o640)
}
