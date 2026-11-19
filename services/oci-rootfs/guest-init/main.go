// Command guest-init is PID 1 of a VM booted from an OCI image. It does the jobs
// that systemd, cloud-init and sshd do in an Ubuntu image, because an OCI image
// has none of them, and it starts the image entrypoint.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/signal"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

const (
	imageConfigurationPath = "/.atlas/image.json"

	// entrypointMaximumDelay caps the wait before the restart of a failing entrypoint.
	entrypointMaximumDelay = time.Minute

	// stopTimeout and shutdownTimeout stay below the Metal stop timeout, so the file
	// system is clean when Metal kills the guest.
	stopTimeout     = 15 * time.Second
	shutdownTimeout = 5 * time.Second

	// signalRealTimeMinimum is SIGRTMIN as programs see it. glibc and musl keep
	// the first real-time signals for themselves.
	signalRealTimeMinimum = 34
	signalMaximum         = 64
)

// imageConfiguration is the configuration object of the OCI image.
type imageConfiguration struct {
	Entrypoint []string
	Cmd        []string
	Env        []string
	WorkingDir string
	User       string
	StopSignal string
}

func main() {
	if os.Getpid() != 1 && len(os.Args) > 1 && os.Args[1] == sftpServerArgument {
		if err := serveSFTP(); err != nil {
			fmt.Fprintln(os.Stderr, "guest-init: sftp:", err)
			os.Exit(1)
		}
		return
	}

	mountKernelFileSystems()
	if err := configureNetwork(); err != nil {
		fmt.Println("guest-init: network:", err)
	}
	stopRequest := make(chan os.Signal, 1)
	signal.Notify(stopRequest, syscall.SIGINT)
	// Metal stops a guest with Ctrl+Alt+Del. The kernel reboots at once unless PID 1 asks for SIGINT.
	if err := syscall.Reboot(syscall.LINUX_REBOOT_CMD_CAD_OFF); err != nil {
		fmt.Println("guest-init: ctrl-alt-del:", err)
	}

	configuration, err := loadImageConfiguration()
	if err != nil {
		fmt.Println("guest-init:", err)
	}

	go reapProcesses()
	metadata := newMetadataClient()
	go metadata.watch()
	go forwardPorts(metadata)
	go serveSSH(metadata, configuration)
	go runSerialShell(metadata, configuration)

	runEntrypoint(configuration, metadata, stopRequest)
	powerOff()
}

func loadImageConfiguration() (imageConfiguration, error) {
	var configuration imageConfiguration
	data, err := os.ReadFile(imageConfigurationPath)
	if err != nil {
		return configuration, fmt.Errorf("read image configuration: %w", err)
	}
	if err := json.Unmarshal(data, &configuration); err != nil {
		return configuration, fmt.Errorf("decode image configuration: %w", err)
	}
	return configuration, nil
}

// environment returns the image variables with the values from the VM metadata.
func (configuration imageConfiguration) environment(attributes map[string]string) []string {
	result := slices.Clone(configuration.Env)
	for key, value := range attributes {
		if name, found := strings.CutPrefix(key, "env."); found {
			result = setVariables(result, name+"="+value)
		}
	}
	return result
}

// setVariables replaces or adds each NAME=value variable.
func setVariables(environment []string, variables ...string) []string {
	result := slices.Clone(environment)
	for _, variable := range variables {
		name, _, _ := strings.Cut(variable, "=")
		result = slices.DeleteFunc(result, func(entry string) bool { return strings.HasPrefix(entry, name+"=") })
		result = append(result, variable)
	}
	return result
}

// runEntrypoint keeps the entrypoint running until Metal asks the guest to stop.
// A clean exit leaves the VM up, so an image such as ubuntu still has its consoles.
func runEntrypoint(configuration imageConfiguration, metadata *metadataClient, stopRequest <-chan os.Signal) {
	delay := time.Second
	for {
		startedAt := time.Now()
		process, exited, err := startEntrypoint(configuration, metadata)
		if err != nil {
			fmt.Println("guest-init: start entrypoint:", err)
		} else {
			select {
			case <-stopRequest:
				stopEntrypoint(process, exited, configuration.StopSignal)
				return
			case status := <-exited:
				fmt.Println("guest-init: entrypoint exited with code", status.ExitStatus())
				if status.Exited() && status.ExitStatus() == 0 {
					<-stopRequest
					return
				}
				// A restart must not run beside the descendants of the failed entrypoint.
				_ = syscall.Kill(-process.Pid, syscall.SIGKILL)
			}
		}

		if time.Since(startedAt) > entrypointMaximumDelay {
			delay = time.Second
		}
		select {
		case <-stopRequest:
			return
		case <-time.After(delay):
		}
		delay = min(delay*2, entrypointMaximumDelay)
	}
}

func stopEntrypoint(process *os.Process, exited <-chan syscall.WaitStatus, stopSignal string) {
	selected := parseSignal(stopSignal)
	_ = process.Signal(selected)
	select {
	case <-exited:
	case <-time.After(stopTimeout):
		fmt.Println("guest-init: entrypoint ignored", selected)
	}
}

// parseSignal reads an image stop signal, such as SIGQUIT, QUIT, 3, or SIGRTMIN+3.
func parseSignal(name string) syscall.Signal {
	if name == "" {
		return syscall.SIGTERM
	}
	if number, err := strconv.Atoi(name); err == nil && number > 0 && number <= signalMaximum {
		return syscall.Signal(number)
	}
	name = strings.ToUpper(name)
	if !strings.HasPrefix(name, "SIG") {
		name = "SIG" + name
	}
	if offset, found := strings.CutPrefix(name, "SIGRTMIN+"); found {
		if number, err := strconv.ParseUint(offset, 10, 8); err == nil && signalRealTimeMinimum+number <= signalMaximum {
			return syscall.Signal(signalRealTimeMinimum + number)
		}
	}
	if signal := unix.SignalNum(name); signal != 0 {
		return signal
	}
	fmt.Printf("guest-init: unknown stop signal %q, using SIGTERM\n", name)
	return syscall.SIGTERM
}

// powerOff ends every process and restarts the machine, which ends Firecracker. A guest has no power-off device.
func powerOff() {
	_ = syscall.Kill(-1, syscall.SIGTERM)
	for deadline := time.Now().Add(shutdownTimeout); time.Now().Before(deadline) && syscall.Kill(-1, 0) == nil; {
		time.Sleep(50 * time.Millisecond)
	}
	_ = syscall.Kill(-1, syscall.SIGKILL)
	syscall.Sync()
	if err := syscall.Mount("", "/", "", syscall.MS_REMOUNT|syscall.MS_RDONLY, ""); err != nil {
		fmt.Println("guest-init: remount root:", err)
	}
	_ = syscall.Reboot(syscall.LINUX_REBOOT_CMD_RESTART)
}

// mountKernelFileSystems mounts only what the kernel provides. Docker does not hide
// image directories such as /run, and images rely on their content.
func mountKernelFileSystems() {
	mounts := []struct {
		source, target, kind string
		flags                uintptr
		data                 string
	}{
		{"proc", "/proc", "proc", syscall.MS_NOSUID | syscall.MS_NODEV | syscall.MS_NOEXEC, ""},
		{"sys", "/sys", "sysfs", syscall.MS_NOSUID | syscall.MS_NODEV | syscall.MS_NOEXEC, ""},
		{"devtmpfs", "/dev", "devtmpfs", syscall.MS_NOSUID, "mode=0755"},
		{"devpts", "/dev/pts", "devpts", syscall.MS_NOSUID | syscall.MS_NOEXEC, "mode=0620,ptmxmode=0666"},
		{"shm", "/dev/shm", "tmpfs", syscall.MS_NOSUID | syscall.MS_NODEV, ""},
		{"cgroup2", "/sys/fs/cgroup", "cgroup2", syscall.MS_NOSUID | syscall.MS_NODEV | syscall.MS_NOEXEC, ""},
	}
	for _, mount := range mounts {
		_ = os.MkdirAll(mount.target, 0o755)
		if err := syscall.Mount(mount.source, mount.target, mount.kind, mount.flags, mount.data); err != nil && err != syscall.EBUSY {
			fmt.Printf("guest-init: mount %s: %v\n", mount.target, err)
		}
	}
	for link, target := range map[string]string{"/dev/fd": "/proc/self/fd", "/dev/stdin": "/proc/self/fd/0", "/dev/stdout": "/proc/self/fd/1", "/dev/stderr": "/proc/self/fd/2"} {
		_ = os.Symlink(target, link)
	}
	for _, directory := range []string{"/run", "/tmp", "/root"} {
		_ = os.MkdirAll(directory, 0o755)
	}
	_ = os.Chmod("/tmp", 0o1777)
	limit := syscall.Rlimit{Cur: 1 << 20, Max: 1 << 20}
	if err := syscall.Setrlimit(syscall.RLIMIT_NOFILE, &limit); err != nil {
		fmt.Println("guest-init: file limit:", err)
	}
}
