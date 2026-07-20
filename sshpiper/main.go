package main

import (
	"github.com/tg123/sshpiper/libplugin"
	"github.com/tg123/sshpiper/libplugin/skel"
	"github.com/urfave/cli/v2"
)

func main() {
	libplugin.CreateAndRunPluginTemplate(&libplugin.PluginTemplate{
		Name:  "atlas",
		Usage: "sshpiperd Atlas lookup plugin",
		Flags: []cli.Flag{
			&cli.StringFlag{
				Name:    "atlas-url",
				Usage:   "base URL of the Atlas/Frappe site",
				EnvVars: []string{"ATLAS_URL"},
			},
			&cli.StringFlag{
				Name:    "gateway",
				Usage:   "Atlas Virtual Machine name this gateway token is scoped to",
				EnvVars: []string{"SSHPIPER_GATEWAY"},
			},
			&cli.StringFlag{
				Name:    "api-key",
				Usage:   "per-gateway Atlas lookup token",
				EnvVars: []string{"SSHPIPER_API_KEY"},
			},
			&cli.StringFlag{
				Name:    "private-key",
				Usage:   "private key used to authenticate to guest VMs",
				Value:   "/root/.ssh/id_ed25519",
				EnvVars: []string{"SSHPIPER_PRIVATE_KEY"},
			},
			&cli.StringFlag{
				Name:    "target-user",
				Usage:   "SSH user used for the upstream guest connection",
				Value:   "root",
				EnvVars: []string{"SSHPIPER_TARGET_USER"},
			},
		},
		CreateConfig: func(c *cli.Context) (*libplugin.SshPiperPluginConfig, error) {
			factory := atlasFactory{
				atlasURL:       c.String("atlas-url"),
				gateway:        c.String("gateway"),
				apiKey:         c.String("api-key"),
				privateKeyPath: c.String("private-key"),
				targetUser:     c.String("target-user"),
			}

			skelPlugin := skel.NewSkelPlugin(factory.listPipe)
			config := skelPlugin.CreateConfig()
			config.NextAuthMethodsCallback = func(conn libplugin.ConnMetadata) ([]string, error) {
				return []string{"publickey"}, nil
			}
			return config, nil
		},
	})
}
