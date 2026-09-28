package storage

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"
)

func TestLinkBootArtifactsIncludesAnOptionalInitrd(t *testing.T) {
	for _, testCase := range []struct {
		name       string
		withInitrd bool
	}{
		{"plain", false},
		{"initrd", true},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			store := NewStores(t.Context(), "metal", t.TempDir(), nil).Images
			if err := os.MkdirAll(store.imageDirectory("ubuntu"), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(store.kernelFile("ubuntu"), []byte("kernel"), 0o644); err != nil {
				t.Fatal(err)
			}
			manifest := imageManifest{
				RootfsSHA256:    strings.Repeat("a", 64),
				KernelSHA256:    strings.Repeat("b", 64),
				RootfsSizeBytes: 1 << 30,
				Architecture:    runtime.GOARCH,
			}
			if testCase.withInitrd {
				manifest.InitrdSHA256 = strings.Repeat("c", 64)
				if err := os.WriteFile(store.initrdFile("ubuntu"), []byte("initrd"), 0o644); err != nil {
					t.Fatal(err)
				}
			}
			if err := store.saveImageManifest("ubuntu", manifest); err != nil {
				t.Fatal(err)
			}

			chroot := t.TempDir()
			initrd, size, err := store.linkBootArtifacts("ubuntu", chroot)
			if err != nil {
				t.Fatal(err)
			}
			if size != manifest.RootfsSizeBytes {
				t.Fatalf("rootfs size = %d, want %d", size, manifest.RootfsSizeBytes)
			}
			assertSameFile(t, store.kernelFile("ubuntu"), filepath.Join(chroot, "vmlinux"))
			if testCase.withInitrd {
				if initrd != "/initrd" {
					t.Fatalf("initrd = %q, want /initrd", initrd)
				}
				assertSameFile(t, store.initrdFile("ubuntu"), filepath.Join(chroot, "initrd"))
			} else if initrd != "" {
				t.Fatalf("plain image initrd = %q", initrd)
			}
		})
	}
}

func TestLinkBootArtifactsRejectsAMissingInitrd(t *testing.T) {
	store := NewStores(t.Context(), "metal", t.TempDir(), nil).Images
	if err := os.MkdirAll(store.imageDirectory("ubuntu"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(store.kernelFile("ubuntu"), []byte("kernel"), 0o644); err != nil {
		t.Fatal(err)
	}
	manifest := imageManifest{
		RootfsSHA256: strings.Repeat("a", 64),
		KernelSHA256: strings.Repeat("b", 64),
		InitrdSHA256: strings.Repeat("c", 64),
		Architecture: runtime.GOARCH,
	}
	if err := store.saveImageManifest("ubuntu", manifest); err != nil {
		t.Fatal(err)
	}

	_, _, err := store.linkBootArtifacts("ubuntu", t.TempDir())
	if !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("error = %v, want file not found", err)
	}
}

func assertSameFile(t *testing.T, first, second string) {
	t.Helper()
	firstInformation, err := os.Stat(first)
	if err != nil {
		t.Fatal(err)
	}
	secondInformation, err := os.Stat(second)
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(firstInformation, secondInformation) {
		t.Fatalf("%s and %s do not share one inode", first, second)
	}
}

func TestLinkOrCopyUsesHardLinkOnOneFileSystem(t *testing.T) {
	directory := t.TempDir()
	source := filepath.Join(directory, "source")
	destination := filepath.Join(directory, "destination")
	if err := os.WriteFile(source, []byte("guest-memory"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := LinkOrCopy(context.Background(), source, destination); err != nil {
		t.Fatal(err)
	}

	content, err := os.ReadFile(destination)
	if err != nil || string(content) != "guest-memory" {
		t.Fatalf("destination = %q, error %v", content, err)
	}
	sourceInformation, err := os.Stat(source)
	if err != nil {
		t.Fatal(err)
	}
	destinationInformation, err := os.Stat(destination)
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(sourceInformation, destinationInformation) {
		t.Error("local files do not share one inode")
	}
}

func TestKernelArgumentsUsesFileValueWhenPresent(t *testing.T) {
	directory := t.TempDir()
	if got := kernelArguments(directory); got != defaultKernelArguments {
		t.Errorf("default = %q", got)
	}
	if err := os.WriteFile(filepath.Join(directory, "boot-args"), []byte("custom args\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := kernelArguments(directory); got != "custom args" {
		t.Errorf("override = %q", got)
	}
}

func TestParseCloneList(t *testing.T) {
	cases := []struct {
		name   string
		output string
		want   []string
	}{
		{"no snapshots", "", nil},
		{"only empty markers", "-\n-\n", nil},
		{"one clone", "metal/staging/snap-1\n-\n", []string{"metal/staging/snap-1"}},
		{
			"multiple clones on one snapshot",
			"metal/staging/a,metal/staging/b\n",
			[]string{"metal/staging/a", "metal/staging/b"},
		},
		{
			"clones across snapshots with blanks",
			"metal/staging/a\n\n-\nmetal/staging/b\n",
			[]string{"metal/staging/a", "metal/staging/b"},
		},
	}

	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if got := parseCloneList(testCase.output); !slices.Equal(got, testCase.want) {
				t.Errorf("parseCloneList(%q) = %v, want %v", testCase.output, got, testCase.want)
			}
		})
	}
}

// fakeZFS puts a recording zfs command first on PATH. Each invocation appends
// its arguments to a log file, `zfs get` prints the clones file, and a
// subcommand named in failures exits non-zero.
func fakeZFS(t *testing.T, clones string, failures ...string) string {
	t.Helper()
	directory := t.TempDir()
	logFile := filepath.Join(directory, "commands.log")
	clonesFile := filepath.Join(directory, "clones")
	if err := os.WriteFile(clonesFile, []byte(clones), 0o644); err != nil {
		t.Fatal(err)
	}

	script := fmt.Sprintf(`#!/bin/sh
echo "$*" >> %q
case "$1" in
get) cat %q ;;
%s) exit 1 ;;
esac
`, logFile, clonesFile, strings.Join(failures, "|"))
	if err := os.WriteFile(filepath.Join(directory, "zfs"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", directory+string(os.PathListSeparator)+os.Getenv("PATH"))

	return logFile
}

func commandLog(t *testing.T, logFile string) []string {
	t.Helper()
	content, err := os.ReadFile(logFile)
	if err != nil {
		t.Fatal(err)
	}

	return strings.Split(strings.TrimSpace(string(content)), "\n")
}

func TestReleasePromotesStagingClonesBeforeDestroy(t *testing.T) {
	logFile := fakeZFS(t, "metal/staging/snap-1\nmetal/vms/vm-2\n-\n", "none")
	store := &VirtualMachineStore{pool: &ZFSPool{name: "metal"}}

	if err := store.Release(context.Background(), "vm-1"); err != nil {
		t.Fatal(err)
	}

	want := []string{
		"get -Hp -r -t snapshot -o value clones metal/vms/vm-1",
		"promote metal/staging/snap-1",
		"destroy -r metal/vms/vm-1",
	}
	if got := commandLog(t, logFile); !slices.Equal(got, want) {
		t.Errorf("commands = %v, want %v", got, want)
	}
}

func TestReleaseFailsWhenPromoteFails(t *testing.T) {
	logFile := fakeZFS(t, "metal/staging/snap-1\n", "promote")
	store := &VirtualMachineStore{pool: &ZFSPool{name: "metal"}}

	err := store.Release(context.Background(), "vm-1")
	if err == nil || !strings.Contains(err.Error(), "promote dependent clone metal/staging/snap-1") {
		t.Fatalf("error = %v", err)
	}
	if got := commandLog(t, logFile); slices.Contains(got, "destroy -r metal/vms/vm-1") {
		t.Error("the VM dataset was destroyed after a failed promotion")
	}
}
