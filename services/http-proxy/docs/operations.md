# Operations

## Setup and configure

Build the VM as root:

```sh
sudo ./nginx/setup.sh
sudo install -d -m 0750 -o root -g nginx /etc/default
sudo install -m 0640 -o root -g nginx control/atlas-proxy-control.env.example \
  /etc/default/atlas-proxy-control
sudoedit /etc/default/atlas-proxy-control
```

Set a long random bcrypt password in `/etc/atlas/proxy-control.htpasswd`. Change `ATLAS_CONTROL_PORT` if the default port is already used. The daemon binds to localhost unless `ATLAS_CONTROL_HOST` is changed.

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

After restarting OpenResty, `readyz` may briefly return `503` while OpenResty loads its persisted maps. It returns `204` when the admin API is available.

Restarting the control daemon does not change proxy configuration. The daemon reads current maps from OpenResty when `/v1/state` is requested and forwards later changes directly to the admin API.

## Logs and state

```sh
journalctl -u atlas-proxy-control.service -f
journalctl -u openresty.service -f
```

The control daemon runs as root because certificate replacement and OpenResty reload require root privileges. OpenResty workers still run as `nginx`. OpenResty must be able to read and write its persisted map files under `/var/lib/nginx`.
