# Build the proxy stack the SAME way the guest does — by running build.sh — so
# the docker-compose test harness exercises byte-identical conf + lua + nginx as
# a real proxy VM (proxy-design.md §9). Docker is used ONLY for this test; the
# shipped artifact is a VM snapshot, never this image.
FROM ubuntu:24.04

# Copy the whole proxy source tree so build.sh finds its sibling conf/lua/html.
COPY . /src/proxy
RUN chmod +x /src/proxy/build.sh && /src/proxy/build.sh

# A test region so router.lua strips ".test.local". build.sh wrote an empty
# region file and aimed the flat cert symlink at certs/_placeholder; here we do
# what build_proxy/push_cert do in the real flow — write the region and repoint
# the flat symlink at certs/<region>/. There's no real wildcard for the harness
# (the TLS tests use curl -k), so the region's cert dir just reuses the
# build-time placeholder PEMs. This keeps the gate faithful to the symlink path.
RUN printf 'test\n' > /var/lib/atlas-proxy/region \
    && mkdir -p /var/lib/atlas-proxy/certs/test \
    && cp /var/lib/atlas-proxy/certs/_placeholder/fullchain.pem /var/lib/atlas-proxy/certs/test/fullchain.pem \
    && cp /var/lib/atlas-proxy/certs/_placeholder/privkey.pem   /var/lib/atlas-proxy/certs/test/privkey.pem \
    && ln -sfn test/fullchain.pem /var/lib/atlas-proxy/certs/fullchain.pem \
    && ln -sfn test/privkey.pem   /var/lib/atlas-proxy/certs/privkey.pem

# Foreground nginx (no systemd in this container). The admin socket and pidfile
# land under /run/atlas-proxy, created by build.sh. tmpfiles isn't run here, so
# ensure the dir exists at start.
ENTRYPOINT ["/bin/sh", "-c", "mkdir -p /run/atlas-proxy && exec /opt/atlas-proxy/sbin/nginx -g 'daemon off;' -c /opt/atlas-proxy/conf/nginx.conf"]
