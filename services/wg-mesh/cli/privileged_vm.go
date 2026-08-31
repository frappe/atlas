package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"sort"

	"github.com/cilium/ebpf"
	"github.com/spf13/cobra"
)

const privilegedTenantAllowedAddressesMap = "privileged_tenant_allowed_addresses"

var privilegedVMAddress string

var listPrivilegedVMJSON bool

var privilegedVMCommand = &cobra.Command{
	Use:   "privileged-vm",
	Short: "manage privileged VMs allowed to access other tenants",
	Args:  cobra.NoArgs,
	RunE:  showHelp,
}

var addPrivilegedVMCommand = &cobra.Command{
	Use:   "add",
	Short: "allow a privileged VM to access other tenants",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return addPrivilegedVM(privilegedVMAddress)
	},
}

var removePrivilegedVMCommand = &cobra.Command{
	Use:   "remove",
	Short: "remove a privileged VM's cross-tenant access",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return removePrivilegedVM(privilegedVMAddress)
	},
}

var listPrivilegedVMCommand = &cobra.Command{
	Use:   "list",
	Short: "list privileged VMs allowed to access other tenants",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return listPrivilegedVMs()
	},
}

func addPrivilegedVM(addressText string) error {
	address, err := parsePrivilegedTenantAddress(addressText)
	if err != nil {
		return err
	}
	allowlist, err := openMap(privilegedTenantAllowedAddressesMap)
	if err != nil {
		return err
	}
	defer allowlist.Close()
	if err := allowlist.Put(address, uint8(1)); err != nil {
		return err
	}
	fmt.Printf("Privileged VM %s can access other tenants\n", netip.AddrFrom16(address))
	return nil
}

func removePrivilegedVM(addressText string) error {
	address, err := parsePrivilegedTenantAddress(addressText)
	if err != nil {
		return err
	}
	allowlist, err := openMap(privilegedTenantAllowedAddressesMap)
	if err != nil {
		return err
	}
	defer allowlist.Close()
	if err := deletePrivilegedVMAddress(allowlist, address); err != nil {
		return err
	}
	fmt.Printf("Privileged VM %s can no longer access other tenants\n", netip.AddrFrom16(address))
	return nil
}

func listPrivilegedVMs() error {
	addresses, err := privilegedTenantAllowedAddresses()
	if err != nil {
		return err
	}
	if listPrivilegedVMJSON {
		return json.NewEncoder(os.Stdout).Encode(privilegedVMList(addresses))
	}
	for _, address := range addresses {
		fmt.Println(address)
	}
	return nil
}

type privilegedVMListItem struct {
	Address string `json:"address"`
}

func privilegedVMList(addresses []string) []privilegedVMListItem {
	items := make([]privilegedVMListItem, len(addresses))
	for index, address := range addresses {
		items[index] = privilegedVMListItem{Address: address}
	}
	return items
}

func privilegedTenantAllowedAddresses() ([]string, error) {
	allowlist, err := openMap(privilegedTenantAllowedAddressesMap)
	if err != nil {
		return nil, err
	}
	defer allowlist.Close()

	addresses := make([]netip.Addr, 0)
	var address [16]byte
	var value uint8
	iterator := allowlist.Iterate()
	for iterator.Next(&address, &value) {
		addresses = append(addresses, netip.AddrFrom16(address))
	}
	if err := iterator.Err(); err != nil {
		return nil, err
	}
	sort.Slice(addresses, func(left, right int) bool {
		return addresses[left].Less(addresses[right])
	})
	entries := make([]string, len(addresses))
	for index, address := range addresses {
		entries[index] = address.String()
	}
	return entries, nil
}

func parsePrivilegedTenantAddress(addressText string) ([16]byte, error) {
	address, err := parseMeshAddress(addressText)
	if err != nil {
		return [16]byte{}, err
	}
	if meshTenant(address) != 0 {
		return [16]byte{}, fmt.Errorf("%q is not a privileged-tenant VM address", addressText)
	}
	return address, nil
}

func deletePrivilegedVMAddress(allowlist *ebpf.Map, address [16]byte) error {
	err := allowlist.Delete(address)
	if errors.Is(err, ebpf.ErrKeyNotExist) {
		return nil
	}
	return err
}
