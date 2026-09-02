# Server SSH tasks

`SSHRunner` runs a shell command on a server through SSH. It returns the command output and exit code.

Use `ServerSSHTask.create_for_command` for a command. Use `ServerSSHTask.create_for_script_file` for a script in `atlas/scripts/`.

Pass script paths relative to `atlas/scripts/`. Paths must not be absolute and must not contain `..`.

An SSH task has one of these states: `Pending`, `Running`, `Success`, or `Failed`.

Tasks run in the `long` queue by default. Set `run_in_background=False` when the caller must wait for the result.

The task stores output in `output` while the command runs. `timeout_seconds` controls the worker and SSH timeout.

The scheduler marks an overdue task as `Failed` after an additional 10-second buffer.
