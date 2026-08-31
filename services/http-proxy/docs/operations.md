# Operations

## Build and configure

Build the VM as root:

```sh
sudo ./nginx/build.sh
sudo install -d -m 0750 -o root -g nginx /etc/default
sudo install -m 0640 -o root -g nginx control/atlas-proxy-control.env.example \
  /etc/default/atlas-proxy-control
sudoedit /etc/default/atlas-proxy-control
```

Set a long random `ATLAS_CONTROL_TOKEN`. Change `ATLAS_CONTROL_PORT` if the
default port is already used. The daemon binds to localhost unless
`ATLAS_CONTROL_HOST` is changed.

Start both services:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now openresty.service atlas-proxy-control.service
```

## Lifecycle checks

```sh
curl -fsS http://127.0.0.1:9000/healthz
curl -fsS -o /dev/null http://127.0.0.1:9000/readyz
sudo systemctl status openresty.service atlas-proxy-control.service
```

After restarting OpenResty, `readyz` may briefly return `503`. The daemon retries
every five seconds and returns `204` after it has restored both maps.

After restarting the daemon, it loads `control-state.json` before starting its
reconciliation loop. A missing or invalid state file is an error and prevents a
misconfigured daemon from claiming readiness.

## Logs and state

```sh
journalctl -u atlas-proxy-control.service -f
journalctl -u openresty.service -f
cat /var/lib/nginx/control-state.json
```

The control daemon must be able to read `/run/nginx/admin.sock` and write
`/var/lib/nginx/control-state.json`. The build creates both paths for the
`nginx` service account.
