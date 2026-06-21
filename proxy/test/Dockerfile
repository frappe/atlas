# Build the proxy stack the SAME way the guest does — by running build.sh — so
# the docker-compose test harness exercises byte-identical conf + lua + nginx as
# a real proxy VM (proxy-design.md §9). Docker is used ONLY for this test; the
# shipped artifact is a VM snapshot, never this image.
#
# The base is plain ubuntu:24.04 (the guest's Ubuntu): build.sh itself adds the
# official nginx.org apt repo, installs the stock nginx, and compiles only the
# dynamic modules. So this gate proves the exact apt-base + dynamic-module stack
# the real proxy guest runs — repo reachability, module ABI, cjson cpath, the lot.
FROM ubuntu:24.04

# Copy the whole proxy source tree so build.sh finds its sibling conf/lua/html.
COPY . /src/proxy
RUN chmod +x /src/proxy/build.sh && /src/proxy/build.sh
# build.sh installs python3 + /usr/local/bin/stream-admin (the stream{} admin
# line-protocol client, spec/17-tcp-proxy.md), so test_tcp.py drives the SAME
# binary the controller runs over SSH — nothing extra to add here.

# A test region so router.lua strips ".test.local". build.sh wrote an empty
# region file and aimed the flat cert symlink at certs/_placeholder; here we do
# what build_proxy/push_cert do in the real flow — write the region and repoint
# the flat symlink at certs/<region>/. There's no real wildcard for the harness
# (the TLS tests use curl -k), so the region's cert dir just reuses the
# build-time placeholder PEMs. This keeps the gate faithful to the symlink path.
RUN printf 'test\n' > /var/lib/nginx/region \
    && mkdir -p /var/lib/nginx/certs/test \
    && cp /var/lib/nginx/certs/_placeholder/fullchain.pem /var/lib/nginx/certs/test/fullchain.pem \
    && cp /var/lib/nginx/certs/_placeholder/privkey.pem   /var/lib/nginx/certs/test/privkey.pem \
    && ln -sfn test/fullchain.pem /var/lib/nginx/certs/fullchain.pem \
    && ln -sfn test/privkey.pem   /var/lib/nginx/certs/privkey.pem

# Foreground nginx (no systemd in this container). The admin socket lands under
# /run/nginx (created by build.sh) and the pidfile at /run/nginx.pid. tmpfiles
# isn't run here, so ensure the socket dir exists at start.
ENTRYPOINT ["/bin/sh", "-c", "mkdir -p /run/nginx && exec /usr/sbin/nginx -g 'daemon off;' -c /etc/nginx/nginx.conf"]
