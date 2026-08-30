package main

import (
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

const vmLockPath = "/run/lock/atlas-wg-mesh.lock"

func lockVMState() (func(), error) {
	if err := os.MkdirAll(filepath.Dir(vmLockPath), 0755); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(vmLockPath, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX); err != nil {
		file.Close()
		return nil, fmt.Errorf("lock VM state: %w", err)
	}
	return func() {
		_ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
		_ = file.Close()
	}, nil
}
