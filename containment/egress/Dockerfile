# Egress proxy - the lineage's only door to the network.
#
# The agent container sits on an `internal: true` Docker network with no route to
# the internet. Its ONLY peer is this proxy, which forwards HTTPS CONNECT to a
# hard allowlist (the provider host) and denies everything else by default. This
# is the "egress default-deny, allow only the provider API endpoint" control.
FROM alpine:3.20

RUN apk add --no-cache tinyproxy

COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY filter        /etc/tinyproxy/filter

EXPOSE 8888
# Run in the foreground so the container's lifecycle tracks the proxy.
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
