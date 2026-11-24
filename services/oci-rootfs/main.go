// Command oci-rootfs builds a bootable ext4 root file system from an OCI image.
// It needs Linux, root for file owners, and mkfs.ext4.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "oci-rootfs:", err)
		os.Exit(1)
	}
}

func run(arguments []string) error {
	if len(arguments) == 0 || arguments[0] != "build" {
		return errors.New("usage: oci-rootfs build [-output path] [-upload-url url -result-url url] reference")
	}
	flags := flag.NewFlagSet("build", flag.ExitOnError)
	output := flags.String("output", "rootfs.ext4", "path of the ext4 file")
	uploadURL := flags.String("upload-url", "", "signed URL that receives the zstd copy of the file")
	resultURL := flags.String("result-url", "", "signed URL that receives the JSON result")
	if err := flags.Parse(arguments[1:]); err != nil {
		return err
	}
	if flags.NArg() != 1 {
		return errors.New("build needs one image reference")
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	err := buildRootfs(ctx, flags.Arg(0), *output)
	if *uploadURL == "" {
		return err
	}
	var result buildResult
	if err == nil {
		result, err = upload(ctx, *output, *uploadURL)
	}
	if err != nil {
		result = buildResult{Error: err.Error()}
	}
	return errors.Join(err, putResult(ctx, *resultURL, result))
}
