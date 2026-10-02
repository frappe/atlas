FROM ubuntu:24.04

COPY http-proxy /src/proxy
COPY control-cluster /src/proxy/control-cluster
RUN chmod +x /src/proxy/nginx/setup.sh && /src/proxy/nginx/setup.sh

RUN printf 'test.x.frappe.dev\n' > /var/lib/nginx/region \
    && printf 'proxy-001\n' > /var/lib/nginx/control-subdomain \
    && printf 'fd00:a71a\nsite-,*-vm-\n' > /var/lib/nginx/auto-proxy \
    && mkdir -p /var/lib/nginx/certs/test \
    && cp /var/lib/nginx/certs/_placeholder/fullchain.pem /var/lib/nginx/certs/test/fullchain.pem \
    && cp /var/lib/nginx/certs/_placeholder/privkey.pem   /var/lib/nginx/certs/test/privkey.pem \
    && ln -sfn test/fullchain.pem /var/lib/nginx/certs/fullchain.pem \
    && ln -sfn test/privkey.pem   /var/lib/nginx/certs/privkey.pem

ENTRYPOINT ["/bin/sh", "-c", "mkdir -p /run/nginx && exec /usr/local/openresty/nginx/sbin/nginx -g 'daemon off;' -c /etc/nginx/nginx.conf"]
