// Command atlas-guest-agent runs inside each Atlas VM. It applies the per-VM metadata
// and answers the sshd authorized keys lookup.
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/frappe/atlas/metal/internal/guestagent"
)

const usage = "usage: atlas-guest-agent apply-metadata | authorized-keys"

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
	default:
		return fmt.Errorf("unknown command %q; %s", command, usage)
	}
}
