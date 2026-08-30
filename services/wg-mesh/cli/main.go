package main

import (
	"errors"
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

func init() {
	cobra.EnableCommandSorting = false

	configureCommand.Flags().StringVar(&uplinkName, "uplink", "", "physical uplink interface")
	configureCommand.Flags().StringVar(&wireGuardName, "wireguard", "", "WireGuard interface")
	configureCommand.MarkFlagRequired("uplink")
	configureCommand.MarkFlagRequired("wireguard")
	configureCommand.Flags().Uint32Var(&whoHasRate, "who-has-rate", defaultWhoHasRate, "sustained WHO_HAS per second per VM (0 disables)")
	configureCommand.Flags().Uint32Var(&whoHasBurst, "who-has-burst", defaultWhoHasBurst, "discovery burst capacity per VM")

	addVirtualMachineCommand.Flags().StringVar(&addInterfaceName, "interface", "", "VM interface")
	addVirtualMachineCommand.Flags().StringVar(&addAddressText, "address", "", "VM private IPv6 address")
	addVirtualMachineCommand.Flags().Uint32Var(&addMTU, "mtu", defaultVMMTU, "VM interface MTU")
	addVirtualMachineCommand.MarkFlagRequired("interface")
	addVirtualMachineCommand.MarkFlagRequired("address")

	removeVirtualMachineCommand.Flags().StringVar(&removeInterfaceName, "interface", "", "VM interface")
	removeVirtualMachineCommand.Flags().StringVar(&removeAddressText, "address", "", "VM private IPv6 address")
	removeVirtualMachineCommand.MarkFlagRequired("interface")
	removeVirtualMachineCommand.MarkFlagRequired("address")
	listVirtualMachinesCommand.Flags().BoolVar(&listVirtualMachinesJSON, "json", false, "print JSON")
	resetCommand.Flags().BoolVar(&resetForce, "force", false, "detach VM hooks and remove BPF state even when local VM entries remain")

	inspectCommand.Flags().StringVar(&inspectAddressText, "address", "", "VM IPv6 address")
	inspectCommand.MarkFlagRequired("address")

	dumpCommand.Flags().StringVar(&dumpSourceText, "src", "", "source IPv6 address")
	dumpCommand.Flags().StringVar(&dumpDestinationText, "dst", "", "destination IPv6 address")
	dumpCommand.Flags().Uint32Var(&dumpTenant, "tenant", 0, "tenant ID")
	dumpCommand.Flags().StringVar(&dumpActionText, "action", "", "accept, drop, or redirect")

	topCommand.Flags().StringVar(&topSourceText, "src", "", "source IPv6 address")
	topCommand.Flags().StringVar(&topDestinationText, "dst", "", "destination IPv6 address")
	topCommand.Flags().Uint32Var(&topTenant, "tenant", 0, "tenant ID")

	remotePurgeCommand.Flags().StringVar(&remoteHostText, "host", "", "WireGuard IPv6 address")
	remotePurgeCommand.MarkFlagRequired("host")
	upgradeCommand.Flags().BoolVar(&upgradeForce, "force", false, "allow a state-breaking upgrade")

	virtualMachineCommand.AddCommand(addVirtualMachineCommand, removeVirtualMachineCommand, listVirtualMachinesCommand)
	debugCommand.AddCommand(debugStatusCommand, debugEnableCommand, debugDisableCommand, inspectCommand, dumpCommand, topCommand)
	remoteCommand.AddCommand(remotePurgeCommand)
	rootCommand.AddCommand(configureCommand, statusCommand, virtualMachineCommand, remoteCommand, debugCommand, upgradeCommand, versionCommand, resetCommand)
}

func main() {
	if err := rootCommand.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, "atlas-wg-mesh:", err)
		os.Exit(1)
	}
}

func requireRoot(command *cobra.Command, _ []string) error {
	if command.Name() == "version" {
		return nil
	}
	if os.Geteuid() != 0 {
		return errors.New("run this command as root")
	}
	return nil
}

func showHelp(command *cobra.Command, _ []string) error {
	return command.Help()
}
