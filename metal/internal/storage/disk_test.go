package storage

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

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

func TestReleaseCreatedDiskDestroysAfterCancel(t *testing.T) {
	logFile := fakeZFS(t, "-\n", "none")
	store := &VirtualMachineStore{pool: &ZFSPool{name: "metal"}}
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()

	store.releaseCreatedDisk(cancelled, "vm-1", true)

	if got := commandLog(t, logFile); !slices.Contains(got, "destroy -r metal/vms/vm-1") {
		t.Errorf("commands = %v, want a destroy of the created disk", got)
	}
}
