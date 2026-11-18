#!/bin/sh
# Writes the number of peers, and of peers with a handshake in the last 3 minutes, for the Datum agent.
# Reading WireGuard needs root and the agent runs as vector, so a root timer writes the file.
set -eu

now=$(date +%s)
wg show wg0 latest-handshakes | awk -v now="$now" '
	{ peers++; if ($2 > 0 && now - $2 < 180) active++ }
	END { printf "{\"peers\":%d,\"active\":%d}\n", peers, active }
' > /run/atlas-wg-gateway/peers.json.next
mv /run/atlas-wg-gateway/peers.json.next /run/atlas-wg-gateway/peers.json
