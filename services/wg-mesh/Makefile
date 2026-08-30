BPF_OBJECT := cli/atlas-wg-mesh.bpf.o
BPF_SOURCE := bpf/bpf.c
BPF_HEADERS := $(wildcard bpf/*.h)
OUTPUT_DIRECTORY := dist
CLI_NAME := atlas-wg-mesh
# A tag if one exists, else the short commit, with -dirty on uncommitted work.
# Falls back to "dev" in a tree with no commits. Override: make build VERSION=1.2.3
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
GO_BUILD_FLAGS := -trimpath -buildvcs=false -ldflags="-s -w -X main.version=$(VERSION)"

.PHONY: bpf build clean version

bpf: $(BPF_OBJECT)

$(BPF_OBJECT): $(BPF_SOURCE) $(BPF_HEADERS)
	clang -Oz -g -Wall -Werror -target bpfel -c $(BPF_SOURCE) -o $@

build: bpf
	mkdir -p $(OUTPUT_DIRECTORY)
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -C cli $(GO_BUILD_FLAGS) -o ../$(OUTPUT_DIRECTORY)/$(CLI_NAME)-linux-amd64 .
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -C cli $(GO_BUILD_FLAGS) -o ../$(OUTPUT_DIRECTORY)/$(CLI_NAME)-linux-arm64 .

version:
	@echo $(VERSION)

clean:
	rm -rf $(OUTPUT_DIRECTORY) $(BPF_OBJECT)
