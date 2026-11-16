package firecracker

import (
	"bufio"
	"context"
	"net"
	"os"
	"path/filepath"
	"testing"

	"github.com/frappe/atlas/metal/internal/vm"
)

func TestGetGuestUsage(t *testing.T) {
	for name, reply := range map[string]string{
		"guest answers": "OK 1073741824\n{\"load_average\":[1.5,1,0.5]}\n",
		"guest refuses": "",
	} {
		t.Run(name, func(t *testing.T) {
			runtime := &Runtime{configuration: testConfig(t.TempDir())}
			serveGuestAgent(t, runtime.configuration.vsockSocketPath("vm"), "CONNECT 100000\n", reply)

			usage := runtime.GetGuestUsage(context.Background(), vm.RuntimeMachine{ID: "vm"})
			if reply == "" {
				if usage != nil {
					t.Fatalf("usage = %+v, want nil when the guest refuses the connection", usage)
				}
				return
			}
			if usage == nil || usage.LoadAverage != [3]float64{1.5, 1, 0.5} {
				t.Fatalf("usage = %+v", usage)
			}
		})
	}
}

func TestGrowGuestRootFilesystemReturnsAgentError(t *testing.T) {
	runtime := &Runtime{configuration: testConfig(t.TempDir())}
	serveGuestAgent(t, runtime.configuration.vsockSocketPath("vm"), "CONNECT 100002\n", "OK 1\nerror cannot grow a btrfs root file system\n")

	err := runtime.GrowGuestRootFilesystem(context.Background(), vm.RuntimeMachine{ID: "vm"})
	if err == nil || err.Error() != "cannot grow a btrfs root file system" {
		t.Fatalf("error = %v", err)
	}
}

// serveGuestAgent answers one connection like Firecracker and the guest agent.
func serveGuestAgent(t *testing.T, socketPath, wantRequest, reply string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(socketPath), 0o700); err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	go func() {
		connection, err := listener.Accept()
		if err != nil {
			return
		}
		defer connection.Close()
		if request, _ := bufio.NewReader(connection).ReadString('\n'); request == wantRequest {
			_, _ = connection.Write([]byte(reply))
		}
	}()
}
