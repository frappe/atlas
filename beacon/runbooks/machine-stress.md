# Machine under stress

A host or a VM used more than 90% of its memory, or had a 5-minute load over 1.5 for each core, for 15 minutes. The alert key is `memory` or `load`.

1. Open a shell on the machine. Run `top -o %MEM` or `top -o %CPU` to find the process.
2. If one process grew, check its logs and restart it when it leaks.
3. On a host, check Host under stress for pressure: the VMs of the host compete for it. Move a VM away when the host stays full.
4. If the machine is too small for its normal load, plan a larger machine.
