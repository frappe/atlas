# Atlas SSHPiper image

This tree is baked into an ordinary Atlas VM through the `sshpiper` Image Build
recipe. `build.sh` installs SSHPiper and the Atlas Go plugin, moves the gateway
VM's management SSH to port 222, and leaves `sshpiper.service` disabled.

After cloning or provisioning a fleet VM from the promoted image, mark it
`is_sshpiper`, attach a Reserved IPv4, and run its Configure SSHPiper action.
Atlas writes the gateway-scoped API token and guest upstream key, then enables
the service. Public users connect to port 22 of the Reserved IPv4; Atlas manages
the gateway over its VM IPv6 on port 222.
