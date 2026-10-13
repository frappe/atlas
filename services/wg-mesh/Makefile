BPF_OBJECT := cli/atlas-wg-mesh.bpf.o
BPF_SOURCE := bpf/bpf.c
BPF_HEADERS := $(wildcard bpf/*.h)
OUTPUT_DIRECTORY := dist
CLI_NAME := atlas-wg-mesh

# VERSION holds the release version. Bump it for a release. Atlas adds the
# source hash as build metadata, for example: make build VERSION=0.1.0+3f9a1c2e
VERSION ?= $(shell cat VERSION)+dev
GO_BUILD_FLAGS := -trimpath -buildvcs=false -ldflags="-s -w -X main.version=$(VERSION)"

.PHONY: bpf build clean version

bpf: $(BPF_OBJECT)

# Debian keeps the asm headers in a multiarch directory. The bpfel target stops
# clang from searching it, so name it. The directory is absent on other layouts.
BPF_INCLUDE_FLAGS := -I/usr/include/$(shell uname -m)-linux-gnu

$(BPF_OBJECT): $(BPF_SOURCE) $(BPF_HEADERS)
	clang -Oz -g -Wall -Werror $(BPF_INCLUDE_FLAGS) -target bpfel -c $(BPF_SOURCE) -o $@

build: bpf
	mkdir -p $(OUTPUT_DIRECTORY)
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -C cli $(GO_BUILD_FLAGS) -o ../$(OUTPUT_DIRECTORY)/$(CLI_NAME)-linux-amd64 .
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -C cli $(GO_BUILD_FLAGS) -o ../$(OUTPUT_DIRECTORY)/$(CLI_NAME)-linux-arm64 .

version:
	@echo $(VERSION)

clean:
	rm -rf $(OUTPUT_DIRECTORY) $(BPF_OBJECT)
