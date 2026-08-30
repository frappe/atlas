package main

import (
	"fmt"
	"net/netip"

	"github.com/spf13/cobra"
)

var remoteHostText string

var remoteCommand = &cobra.Command{
	Use:   "remote",
	Short: "manage learned remote locations",
	Args:  cobra.NoArgs,
	RunE:  showHelp,
}

var remotePurgeCommand = &cobra.Command{
	Use:   "purge",
	Short: "remove remote entries for a host",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return purgeRemote(remoteHostText)
	},
}

func purgeRemote(hostText string) error {
	address, err := netip.ParseAddr(hostText)
	if err != nil || !address.Is6() {
		return fmt.Errorf("%q is not a WireGuard IPv6 address", hostText)
	}
	return purgeRemoteHost(address.As16())
}

func purgeRemoteHost(host [16]byte) error {
	remoteMap, err := openMap("remote_vms")
	if err != nil {
		return err
	}
	defer remoteMap.Close()
	var vm [16]byte
	var found [16]byte
	iterator := remoteMap.Iterate()
	removed := 0
	for iterator.Next(&vm, &found) {
		if found != host {
			continue
		}
		if err := remoteMap.Delete(vm); err != nil {
			return err
		}
		removed++
	}
	if err := iterator.Err(); err != nil {
		return err
	}
	fmt.Printf("removed %d remote entries\n", removed)
	return nil
}
