package main

import (
	"crypto/sha256"
	"embed"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"

	"github.com/spf13/cobra"
)

// The CLI embeds the kernel module sources. A host builds them against its
// running kernel headers, so the module always matches the running kernel.
//
//go:embed kernel_module
var kernelModuleSources embed.FS

const (
	kernelModuleSourceDirectory = "/usr/src/atlas-wg-mesh-neigh"
	kernelModuleStampFile       = "module-stamp"
	kernelModuleName            = "atlas_neigh"
	kernelModuleFunction        = "atlas_register_neigh"
	kernelModuleLoadFile        = "/etc/modules-load.d/atlas-neigh.conf"
	kernelModuleSymbolsFile     = "/proc/kallsyms"
)

var moduleCommand = &cobra.Command{
	Use:   "module",
	Short: "manage the Atlas neighbour kernel module",
}

var moduleInstallCommand = &cobra.Command{
	Use:   "install",
	Short: "build and load the Atlas neighbour kernel module",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return runModuleInstall()
	},
}

// runModuleInstall builds, links, and loads the Atlas neighbour kernel
// module for the running kernel.
//
// The module must carry a BTF section, or the BPF object cannot resolve the
// kfunc extern. Ubuntu header packages ship no vmlinux file, so the build
// takes the kernel BTF from /sys/kernel/btf/vmlinux.
//
// The linked module in /lib/modules is a symlink to the built file. The
// symlink keeps the built file with its BTF section exactly as the build
// produced it.
//
// A matching stamp skips the build, so a run after a reboot links and loads
// the existing module without compiling it again.
func runModuleInstall() error {
	release, err := commandOutput("uname", "-r")
	if err != nil {
		return err
	}
	release = strings.TrimSpace(release)

	buildDirectory := filepath.Join("/lib/modules", release, "build")
	if information, err := os.Stat(buildDirectory); err != nil || !information.IsDir() {
		return fmt.Errorf("kernel headers for %s are missing; install the linux-headers-%s package and run this command again", release, release)
	}

	for _, tool := range []string{"make", "cc", "pahole"} {
		if _, err := exec.LookPath(tool); err != nil {
			return fmt.Errorf("%s is required to build the kernel module; install the build-essential and dwarves packages", tool)
		}
	}

	stamp := moduleStamp(release)
	storedStamp, stampError := os.ReadFile(filepath.Join(kernelModuleSourceDirectory, kernelModuleStampFile))

	moduleObject := filepath.Join(kernelModuleSourceDirectory, kernelModuleName+".ko")
	_, objectError := os.Stat(moduleObject)

	needBuild := stampError != nil || string(storedStamp) != stamp || objectError != nil

	if needBuild {
		if err := writeKernelModuleSources(kernelModuleSourceDirectory); err != nil {
			return err
		}

		if err := ensureKernelModuleBTFBase(buildDirectory); err != nil {
			return err
		}

		if err := runCommand("make", "-C", buildDirectory, "M="+kernelModuleSourceDirectory, "clean", "modules"); err != nil {
			return err
		}

		if err := os.WriteFile(filepath.Join(kernelModuleSourceDirectory, kernelModuleStampFile), []byte(stamp), 0644); err != nil {
			return err
		}
	} else {
		fmt.Println("Atlas neighbour kernel module sources are current; linking the built module")
	}

	extraDirectory := filepath.Join("/lib/modules", release, "extra")
	if err := os.MkdirAll(extraDirectory, 0755); err != nil {
		return err
	}

	linkPath := filepath.Join(extraDirectory, kernelModuleName+".ko")
	if err := os.Remove(linkPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := os.Symlink(moduleObject, linkPath); err != nil {
		return err
	}

	if err := runCommand("depmod", release); err != nil {
		return err
	}

	if kernelModuleFunctionRegistered() && needBuild {
		// The module can stay in use after its sources changed, for example
		// while a BPF program still runs. The old module then serves the
		// kfunc until the next reboot.
		if err := runCommand("rmmod", kernelModuleName); err != nil {
			fmt.Printf("atlas-wg-mesh: warning: the previous module stays loaded until a reboot: %v\n", err)
		}
	}

	if err := runCommand("modprobe", kernelModuleName); err != nil {
		return err
	}

	if err := os.MkdirAll(filepath.Dir(kernelModuleLoadFile), 0755); err != nil {
		return err
	}
	if err := os.WriteFile(kernelModuleLoadFile, []byte(kernelModuleName+"\n"), 0644); err != nil {
		return err
	}

	if !kernelModuleFunctionRegistered() {
		return errors.New("the Atlas neighbour kernel module loaded but registered no kfunc")
	}

	fmt.Printf("Atlas neighbour kernel module installed for %s\n", release)
	return nil
}

// moduleStamp ties the built module to the embedded sources and the running
// kernel. A change of either one forces a clean build, because kernel build
// output from one release cannot serve another.
func moduleStamp(release string) string {
	digest := sha256.New()

	names, err := kernelModuleSources.ReadDir("kernel_module")
	if err != nil {
		return ""
	}

	sort.Slice(names, func(left, right int) bool {
		return names[left].Name() < names[right].Name()
	})

	for _, name := range names {
		contents, err := kernelModuleSources.ReadFile("kernel_module/" + name.Name())
		if err != nil {
			continue
		}
		digest.Write([]byte(name.Name()))
		digest.Write(contents)
	}

	return fmt.Sprintf("%x:%s", digest.Sum(nil), release)
}

// writeKernelModuleSources places the embedded sources in one directory.
func writeKernelModuleSources(directory string) error {
	if err := os.MkdirAll(directory, 0755); err != nil {
		return err
	}

	names, err := kernelModuleSources.ReadDir("kernel_module")
	if err != nil {
		return err
	}

	for _, name := range names {
		contents, err := kernelModuleSources.ReadFile("kernel_module/" + name.Name())
		if err != nil {
			return err
		}
		if err := os.WriteFile(filepath.Join(directory, name.Name()), contents, 0644); err != nil {
			return err
		}
	}

	return nil
}

// ensureKernelModuleBTFBase provides the vmlinux file that module BTF
// generation needs. The kernel BTF in /sys/kernel/btf/vmlinux serves as the
// base, so a plain Ubuntu host builds a module with a BTF section.
func ensureKernelModuleBTFBase(buildDirectory string) error {
	vmlinux := filepath.Join(buildDirectory, "vmlinux")
	if _, err := os.Stat(vmlinux); err == nil {
		return nil
	}

	if _, err := os.Stat("/sys/kernel/btf/vmlinux"); err != nil {
		return errors.New("the kernel exposes no BTF at /sys/kernel/btf/vmlinux")
	}

	contents, err := os.ReadFile("/sys/kernel/btf/vmlinux")
	if err != nil {
		return fmt.Errorf("read the kernel BTF: %w", err)
	}

	if err := os.WriteFile(vmlinux, contents, 0644); err != nil {
		return fmt.Errorf("place the kernel BTF as %s: %w", vmlinux, err)
	}

	return nil
}

// kernelModuleFunctionRegistered reports whether the module kfunc is live.
func kernelModuleFunctionRegistered() bool {
	symbols, err := os.ReadFile(kernelModuleSymbolsFile)
	if err != nil {
		return false
	}

	return strings.Contains(string(symbols), kernelModuleFunction)
}
