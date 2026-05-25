#!/bin/bash
# Reboot the server. SSH will drop, so this Task ends in Failure with a
# "broken pipe" or similar — operator confirms reboot by waiting and probing.
set -euo pipefail

sudo systemctl reboot
