FROM debian:trixie-slim

ARG GO_VERSION=1.26.9
ARG TARGETARCH

RUN apt-get update \
	&& apt-get install --yes --no-install-recommends \
		binutils ca-certificates clang curl e2fsprogs git kmod libbpf-dev linux-libc-dev llvm make python3 squashfs-tools zstd \
	&& rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" | tar -C /usr/local -xz
ENV PATH=/usr/local/go/bin:$PATH

# The mounted repository belongs to another user, and git refuses it otherwise.
RUN git config --system safe.directory '*'
