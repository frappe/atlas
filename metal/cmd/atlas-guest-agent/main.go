// Command atlas-guest-agent runs inside each Atlas VM. It applies the per-VM metadata,
// answers the sshd authorized keys lookup, and answers metald jobs over vsock.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"

	"github.com/frappe/atlas/metal/internal/guestagent"
)

const usage = "usage: atlas-guest-agent apply-metadata | authorized-keys | report-usage | freeze-filesystems | grow-root-filesystem | reboot"

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, usage)
		os.Exit(2)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, os.Args[1]); err != nil {
		fmt.Fprintln(os.Stderr, "atlas-guest-agent:", err)
		os.Exit(1)
	}
}

func run(ctx context.Context, command string) error {
	switch command {
	case "apply-metadata":
		guestagent.RunMetadata(ctx)
		return nil
	case "authorized-keys":
		return guestagent.WriteAuthorizedKeys(ctx, os.Stdout)
	// systemd passes each accepted vsock connection as standard input and output.
	case "report-usage":
		report, err := guestagent.ReadUsage("/proc")
		if err != nil {
			return err
		}
		return json.NewEncoder(os.Stdout).Encode(report)
	case "freeze-filesystems":
		return guestagent.FreezeFilesystems(ctx, struct {
			io.Reader
			io.Writer
		}{os.Stdin, os.Stdout})
	case "grow-root-filesystem":
		return guestagent.WriteReply(os.Stdout, guestagent.GrowRootFilesystem(ctx))
	case "reboot":
		return guestagent.RequestReboot(ctx, os.Stdout)
	default:
		return fmt.Errorf("unknown command %q; %s", command, usage)
	}
}
