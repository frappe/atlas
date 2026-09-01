#!/usr/bin/env bash

set -eu

sudo -n install -d -m 700 -o root -g root /root/.ssh
sudo -n cp /home/ubuntu/.ssh/authorized_keys /root/.ssh/authorized_keys
sudo -n chown root:root /root/.ssh/authorized_keys
sudo -n chmod 600 /root/.ssh/authorized_keys
sudo -n nohup sh -c 'sleep 2; userdel --remove ubuntu' >/dev/null 2>&1 &
