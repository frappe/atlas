package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var listVirtualMachinesJSON bool

var listVirtualMachinesCommand = &cobra.Command{
	Use:   "list",
	Short: "list local VM ownership",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return listVirtualMachines()
	},
}

func listVirtualMachines() error {
	virtualMachines, err := localVirtualMachines()
	if err != nil {
		return err
	}
	if listVirtualMachinesJSON {
		return json.NewEncoder(os.Stdout).Encode(virtualMachineList(virtualMachines))
	}
	for _, virtualMachine := range virtualMachines {
		fmt.Printf("%s\t%s\n", virtualMachine.address, virtualMachine.interfaceLabel())
	}
	return nil
}

type virtualMachineListItem struct {
	Address   string `json:"address"`
	Interface string `json:"interface"`
}

func virtualMachineList(virtualMachines []localVirtualMachine) []virtualMachineListItem {
	items := make([]virtualMachineListItem, len(virtualMachines))
	for index, virtualMachine := range virtualMachines {
		items[index] = virtualMachineListItem{
			Address:   virtualMachine.address.String(),
			Interface: virtualMachine.interfaceLabel(),
		}
	}
	return items
}
