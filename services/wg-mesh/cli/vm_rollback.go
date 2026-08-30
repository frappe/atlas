package main

import (
	"errors"
	"fmt"
)

func rollbackVirtualMachineRemoval(routeRemoved, hookDetached bool, addressText, interfaceName string, cause error) error {
	rollbackError := cause
	if hookDetached {
		rollbackError = restoreVirtualMachineHook(interfaceName, rollbackError)
	}
	if routeRemoved {
		if err := runCommand("ip", "-6", "route", "replace", addressText+"/128", "dev", interfaceName); err != nil {
			rollbackError = errors.Join(rollbackError, fmt.Errorf("restore route for %s: %w", addressText, err))
		}
	}
	return rollbackError
}

func rollbackVirtualMachineAddition(address [16]byte, ifindex uint32, addressText, interfaceName string, cause error) error {
	if err := removeLocalVirtualMachine(address); err != nil {
		return errors.Join(cause, fmt.Errorf("remove VM registration: %w", err))
	}
	if err := runCommand("ip", "-6", "route", "del", addressText+"/128", "dev", interfaceName); err != nil {
		rollbackError := errors.Join(cause, fmt.Errorf("remove route for %s: %w", addressText, err))
		if restoreErr := addLocalVirtualMachine(address, ifindex); restoreErr != nil {
			rollbackError = errors.Join(rollbackError, fmt.Errorf("restore VM registration: %w", restoreErr))
		}
		return rollbackError
	}
	return cause
}

func restoreVirtualMachineHook(interfaceName string, cause error) error {
	if err := attachHook(interfaceName, vmBPFProgram); err != nil {
		rollbackError := errors.Join(cause, fmt.Errorf("restore hook on %s: %w", interfaceName, err))
		if err := runCommand("ip", "link", "set", interfaceName, "down"); err != nil {
			return errors.Join(rollbackError, fmt.Errorf("isolate unguarded VM interface %s: %w", interfaceName, err))
		}
		return rollbackError
	}
	return cause
}
