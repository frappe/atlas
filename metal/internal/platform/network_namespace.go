package platform

import (
	"errors"
	"fmt"
	"os"
	"runtime"

	"golang.org/x/sys/unix"
)

const currentNamespacePath = "/proc/thread-self/ns/net"

// WithNetworkNamespace runs operation on a thread inside the network namespace at
// path. A socket that operation opens stays in that namespace after it returns.
func WithNetworkNamespace(path string, operation func() error) error {
	runtime.LockOSThread()
	safeToReuseThread := true
	defer func() {
		if safeToReuseThread {
			runtime.UnlockOSThread()
		}
	}()

	currentNamespace, err := os.Open(currentNamespacePath)
	if err != nil {
		return fmt.Errorf("open current network namespace: %w", err)
	}
	defer currentNamespace.Close()

	targetNamespace, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("open network namespace %s: %w", path, err)
	}
	defer targetNamespace.Close()

	if err := unix.Setns(int(targetNamespace.Fd()), unix.CLONE_NEWNET); err != nil {
		return fmt.Errorf("enter network namespace %s: %w", path, err)
	}
	safeToReuseThread = false

	operationError := operation()
	if err := unix.Setns(int(currentNamespace.Fd()), unix.CLONE_NEWNET); err != nil {
		return errors.Join(operationError, fmt.Errorf("restore network namespace: %w", err))
	}
	safeToReuseThread = true
	return operationError
}
