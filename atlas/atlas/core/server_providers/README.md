# Server providers

A server provider manages the remote server and provider resources for Atlas.

## Provider contract

Create a package in this folder. Register the provider class with `@register`.

The class must define `provider_type` and `credential_fields`. Implement the abstract methods in `ServerProvider` for:

- provider setup and credential checks
- server image and size catalog data
- server creation and power actions
- private network and SSH key resources
- the storage device that metald uses for the VM storage pool

`get_storage_pool_device` must return a raw block device with no filesystem or mountpoint for use as the ZFS pool.

Return post-creation functions from `provisioning_steps`. Atlas calls them in order.

Use `wait_for_ssh` to wait for SSH access. Use `run_setup_script` to run a packaged setup script and require a successful result.

Put provider scripts in `atlas/scripts/<provider>/`. Add tests for provider operations and provisioning order.

`create_server` runs before Atlas inserts the `Server` document. `run_provisioning` runs the provisioning steps in a background job.
