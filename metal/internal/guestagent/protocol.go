package guestagent

import (
	"fmt"
	"io"
)

// Each job has its own vsock port, so an old agent refuses a newer job. The ports
// are above 65535, so they cannot clash with a service that reuses a TCP port number.
const (
	UsagePort              = 100000
	FreezePort             = 100001
	GrowRootFilesystemPort = 100002
	RebootPort             = 100003
)

// WriteReply writes "ok", or "error <reason>" when err is not nil.
func WriteReply(writer io.Writer, err error) error {
	if err != nil {
		_, writeError := fmt.Fprintln(writer, "error", err)
		return writeError
	}
	_, writeError := fmt.Fprintln(writer, "ok")
	return writeError
}
