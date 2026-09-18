package main

import "github.com/spf13/cobra"

const pinDirectory = "/sys/fs/bpf/atlas-wg-mesh"

const defaultVMMTU = 1380

var (
	uplinkName, wireGuardName string
	resetForce                bool
)

var rootCommand = &cobra.Command{
	Use:               "atlas-wg-mesh",
	Short:             "connect VMs across hosts with eBPF and WireGuard",
	Long:              "Connect VMs across hosts with eBPF and WireGuard.",
	Args:              cobra.NoArgs,
	RunE:              showHelp,
	PersistentPreRunE: requireRoot,
	CompletionOptions: cobra.CompletionOptions{DisableDefaultCmd: true},
	SilenceUsage:      true,
	SilenceErrors:     true,
}

var configureCommand = &cobra.Command{
	Use:   "configure",
	Short: "configure Atlas WG Mesh on this host",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return installHost(uplinkName, wireGuardName)
	},
}

var resetCommand = &cobra.Command{
	Use:   "reset",
	Short: "remove Atlas WG Mesh from this host",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return removeHost(resetForce)
	},
}

var statusCommand = &cobra.Command{
	Use:   "status",
	Short: "show local Atlas WG Mesh state",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return showStatus()
	},
}

var versionCommand = &cobra.Command{
	Use:   "version",
	Short: "show CLI and BPF versions",
	Args:  cobra.NoArgs,
	RunE: func(*cobra.Command, []string) error {
		return showVersion()
	},
}
