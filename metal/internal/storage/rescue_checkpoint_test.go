package storage

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/frappe/atlas/metal/internal/vm"
)

func TestCheckpointCapacityExcludesUnusedReservation(t *testing.T) {
	const gib = uint64(1 << 30)
	for _, test := range []struct {
		name                                             string
		available, referenced, reservation, unused, pool uint64
		wantAvailable, wantRequired                      uint64
	}{
		{"sparse", 3 * gib, gib, 0, 0, 20 * gib, 3 * gib, gib},
		{"thick", 11 * gib, gib, 10 * gib, 9 * gib, 20 * gib, 2 * gib, 2 * gib},
		{"quota below reservation", gib, gib, 10 * gib, 9 * gib, 20 * gib, 0, 2 * gib},
		{"partly reserved", 4 * gib, 3 * gib, gib, 0, 20 * gib, 4 * gib, 2 * gib},
		{"large pool", 8 * gib, gib, 0, 0, 100 * gib, 8 * gib, 5 * gib},
	} {
		t.Run(test.name, func(t *testing.T) {
			space := checkpointSpace(test.available, test.referenced, test.reservation, test.unused, test.pool)
			if space.AvailableBytes != test.wantAvailable || space.RequiredBytes != test.wantRequired {
				t.Fatalf("space = %+v, want available %d required %d", space, test.wantAvailable, test.wantRequired)
			}
		})
	}
}

func TestCheckpointPreflightBoundaryAndMalformedAccounting(t *testing.T) {
	for _, test := range []struct {
		name, values string
		wantError    bool
		wantSpace    bool
	}{
		{"exact headroom", "1073741824\n0\n0\n0\n", false, false},
		{"one byte below", "1073741823\n0\n0\n0\n", true, true},
		{"missing property", "1073741824\n0\n0\n", true, false},
		{"negative", "-1\n0\n0\n0\n", true, false},
		{"nonnumeric", "unavailable\n0\n0\n0\n", true, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			log := checkpointCommands(t, fmt.Sprintf("get) printf '%%b' %q ;;", test.values))
			store := &VirtualMachineStore{pool: &ZFSPool{name: "metal"}}
			_, err := store.CheckRescueCheckpointSpace(context.Background(), "vm-1")
			if (err != nil) != test.wantError {
				t.Fatalf("error = %v", err)
			}
			var spaceError *vm.RescueCheckpointSpaceError
			if errors.As(err, &spaceError) != test.wantSpace {
				t.Fatalf("space error = %v", err)
			}
			if strings.Contains(strings.Join(commandLog(t, log), "\n"), "snapshot") {
				t.Fatal("preflight mutated storage")
			}
		})
	}
}

func TestCheckpointRollbackRequiresMatchingIdentity(t *testing.T) {
	for _, test := range []struct {
		name, output, guid string
		wantError          bool
	}{
		{"matching", "guid\t42\t-\natlas:rescue-generation\t7\tlocal\n", "42", false},
		{"replaced snapshot", "guid\t43\t-\natlas:rescue-generation\t7\tlocal\n", "42", true},
		{"wrong session", "guid\t42\t-\natlas:rescue-generation\t8\tlocal\n", "42", true},
		{"inherited marker", "guid\t42\t-\natlas:rescue-generation\t7\tinherited from metal/vms\n", "42", true},
		{"missing marker", "guid\t42\t-\n", "42", true},
		{"invalid guid", "guid\tno\t-\natlas:rescue-generation\t7\tlocal\n", "no", true},
		{"no saved guid", "guid\t42\t-\natlas:rescue-generation\t7\tlocal\n", "", true},
	} {
		t.Run(test.name, func(t *testing.T) {
			log := checkpointCommands(t, fmt.Sprintf("get) printf '%%b' %q ;;", test.output))
			store := &VirtualMachineStore{pool: &ZFSPool{name: "metal"}}
			err := store.RollbackRescueCheckpoint(context.Background(), "vm-1", 7, test.guid)
			if (err != nil) != test.wantError {
				t.Fatalf("error = %v", err)
			}
			commands := strings.Join(commandLog(t, log), "\n")
			if strings.Contains(commands, "rollback metal/vms/vm-1@rescue-7") == test.wantError {
				t.Fatalf("commands = %s", commands)
			}
			if strings.Contains(commands, "rollback -") {
				t.Fatal("rollback must not delete newer snapshots")
			}
		})
	}
}

func TestMissingCheckpointFailsRollbackButAllowsCleanup(t *testing.T) {
	log := checkpointCommands(t, "get|list) echo 'dataset does not exist' >&2; exit 1 ;;")
	store := &VirtualMachineStore{pool: &ZFSPool{name: "metal"}}
	if err := store.RollbackRescueCheckpoint(context.Background(), "vm-1", 7, "42"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("rollback error = %v", err)
	}
	if err := store.DiscardRescueCheckpoint(context.Background(), "vm-1", 7, "42"); err != nil {
		t.Fatalf("cleanup error = %v", err)
	}
	if len(commandLog(t, log)) != 2 {
		t.Fatal("missing snapshot caused a storage mutation")
	}
}

func TestCheckpointRetryAdoptsOnlyItsOwnSnapshot(t *testing.T) {
	log := checkpointCommands(t, "get) printf 'guid\\t42\\t-\\natlas:rescue-generation\\t7\\tlocal\\n' ;;")
	store := &VirtualMachineStore{pool: &ZFSPool{name: "metal"}}
	guid, err := store.CreateRescueCheckpoint(context.Background(), "vm-1", 7)
	if err != nil || guid != "42" {
		t.Fatalf("guid = %s, error = %v", guid, err)
	}
	if len(commandLog(t, log)) != 2 {
		t.Fatal("retry created another snapshot or ran space preflight")
	}
}

func TestCheckpointCleanupRejectsReplacement(t *testing.T) {
	log := checkpointCommands(t, "get) printf 'guid\\t43\\t-\\natlas:rescue-generation\\t7\\tlocal\\n' ;;")
	store := &VirtualMachineStore{pool: &ZFSPool{name: "metal"}}
	if err := store.DiscardRescueCheckpoint(context.Background(), "vm-1", 7, "42"); err == nil {
		t.Fatal("cleanup accepted another snapshot")
	}
	for _, command := range commandLog(t, log) {
		if strings.HasPrefix(command, "destroy ") {
			t.Fatal("cleanup deleted another snapshot")
		}
	}
}

func TestCheckpointCreationFailureDoesNotClaimProtection(t *testing.T) {
	log := checkpointCommands(t, `
list) echo 'dataset does not exist' >&2; exit 1 ;;
get) printf '2147483648\n0\n0\n0\n' ;;
snapshot) echo 'out of space' >&2; exit 1 ;;
`)
	store := &VirtualMachineStore{pool: &ZFSPool{name: "metal"}}
	guid, err := store.CreateRescueCheckpoint(context.Background(), "vm-1", 7)
	if err == nil || guid != "" || !strings.Contains(err.Error(), "out of space") {
		t.Fatalf("failed creation = %q, %v", guid, err)
	}
	commands := commandLog(t, log)
	if commands[len(commands)-1] != "snapshot -o atlas:rescue-generation=7 metal/vms/vm-1@rescue-7" {
		t.Fatalf("commands = %v", commands)
	}
}

func TestCheckpointRollbackFailureDoesNotDeleteBaseline(t *testing.T) {
	log := checkpointCommands(t, `
get) printf 'guid\t42\t-\natlas:rescue-generation\t7\tlocal\n' ;;
rollback) echo 'newer snapshots exist' >&2; exit 1 ;;
`)
	store := &VirtualMachineStore{pool: &ZFSPool{name: "metal"}}
	if err := store.RollbackRescueCheckpoint(context.Background(), "vm-1", 7, "42"); err == nil {
		t.Fatal("rollback failure was ignored")
	}
	for _, command := range commandLog(t, log) {
		if strings.HasPrefix(command, "destroy ") {
			t.Fatal("failed rollback deleted the baseline")
		}
	}
}

func checkpointCommands(t *testing.T, cases string) string {
	t.Helper()
	directory := t.TempDir()
	log := filepath.Join(directory, "commands.log")
	zfs := fmt.Sprintf("#!/bin/sh\necho \"$*\" >> %q\ncase \"$1\" in\n%s\nesac\n", log, cases)
	for name, script := range map[string]string{
		"zfs": zfs, "zpool": "#!/bin/sh\necho 21474836480\n",
	} {
		if err := os.WriteFile(filepath.Join(directory, name), []byte(script), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", directory+string(os.PathListSeparator)+os.Getenv("PATH"))
	return log
}
