OUTPUT_DIRECTORY := dist
BINARY_NAME := metald
# A tag if one exists, else the short commit, with -dirty on uncommitted work.
# Falls back to "dev" in a tree with no commits. Override: make build VERSION=1.2.3
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
# -trimpath drops build paths, -s -w drop the symbol table and DWARF.
GO_BUILD_FLAGS := -trimpath -buildvcs=false -ldflags="-s -w -X main.version=$(VERSION)"

.PHONY: build clean version

build:
	mkdir -p $(OUTPUT_DIRECTORY)
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build $(GO_BUILD_FLAGS) -o $(OUTPUT_DIRECTORY)/$(BINARY_NAME)-linux-amd64 ./cmd/metald
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build $(GO_BUILD_FLAGS) -o $(OUTPUT_DIRECTORY)/$(BINARY_NAME)-linux-arm64 ./cmd/metald

version:
	@echo $(VERSION)

clean:
	rm -rf $(OUTPUT_DIRECTORY)
