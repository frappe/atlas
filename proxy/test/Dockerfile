# Build the proxy stack the SAME way the guest does — by running build.sh — so
# the docker-compose test harness exercises byte-identical conf + lua + nginx as
# a real proxy VM (proxy-design.md §9). Docker is used ONLY for this test; the
# shipped artifact is a VM snapshot, never this image.
FROM ubuntu:24.04

# Copy the whole proxy source tree so build.sh finds its sibling conf/lua/html.
COPY . /src/proxy
RUN chmod +x /src/proxy/build.sh && /src/proxy/build.sh

# A test region so router.lua strips ".test.local". build.sh wrote an empty
# region file; overwrite it for the harness.
RUN printf 'test\n' > /var/lib/atlas-proxy/region

# Foreground nginx (no systemd in this container). The admin socket and pidfile
# land under /run/atlas-proxy, created by build.sh. tmpfiles isn't run here, so
# ensure the dir exists at start.
ENTRYPOINT ["/bin/sh", "-c", "mkdir -p /run/atlas-proxy && exec /opt/atlas-proxy/sbin/nginx -g 'daemon off;' -c /opt/atlas-proxy/conf/nginx.conf"]
