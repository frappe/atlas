package main

import (
	"errors"
	"fmt"
)

func rollbackVirtualMachineRemoval(routeRemoved, hookDetached bool, addressText, interfaceName string, cause error) error {
	rollbackError := cause
	if hookDetached {
		if err := attachHook(interfaceName, vmBPFProgram); err != nil {
			rollbackError = errors.Join(rollbackError, fmt.Errorf("restore hook on %s: %w", interfaceName, err))
		}
	}
	if routeRemoved {
		if err := runCommand("ip", "-6", "route", "replace", addressText+"/128", "dev", interfaceName); err != nil {
			rollbackError = errors.Join(rollbackError, fmt.Errorf("restore route for %s: %w", addressText, err))
		}
	}
	return rollbackError
}

func rollbackVirtualMachineAddition(address [16]byte, ifindex uint32, hookAttached bool, addressText, interfaceName string, cause error) error {
	hasOtherVM, err := hasOtherLocalVirtualMachineOnInterface(address, ifindex)
	if err != nil {
		return errors.Join(cause, fmt.Errorf("find other VMs on %s: %w", interfaceName, err))
	}
	hookDetached := false
	if hookAttached && !hasOtherVM {
		if err := detachHook(interfaceName); err != nil {
			return errors.Join(cause, fmt.Errorf("remove hook from %s: %w", interfaceName, err))
		}
		hookDetached = true
	}
	if err := removeLocalVirtualMachine(address); err != nil {
		return restoreVirtualMachineAdditionHook(hookDetached, interfaceName, errors.Join(cause, fmt.Errorf("remove VM registration: %w", err)))
	}
	if err := runCommand("ip", "-6", "route", "del", addressText+"/128", "dev", interfaceName); err != nil {
		rollbackError := errors.Join(cause, fmt.Errorf("remove route for %s: %w", addressText, err))
		if restoreErr := addLocalVirtualMachine(address, ifindex); restoreErr != nil {
			rollbackError = errors.Join(rollbackError, fmt.Errorf("restore VM registration: %w", restoreErr))
			if cleanupErr := runCommand("ip", "-6", "route", "del", addressText+"/128", "dev", interfaceName); cleanupErr != nil {
				rollbackError = errors.Join(rollbackError, fmt.Errorf("remove stale route for %s: %w", addressText, cleanupErr))
			}
			return rollbackError
		}
		return restoreVirtualMachineAdditionHook(hookDetached, interfaceName, rollbackError)
	}
	return cause
}

func restoreVirtualMachineAdditionHook(detached bool, interfaceName string, cause error) error {
	if !detached {
		return cause
	}
	if err := attachHook(interfaceName, vmBPFProgram); err != nil {
		return errors.Join(cause, fmt.Errorf("restore hook on %s: %w", interfaceName, err))
	}
	return cause
}
