OUTPUT_DIRECTORY := dist
SPEC_DIRECTORY := internal/api
# The API specification generator. go run keeps it out of the global toolchain.
SWAG := go run github.com/swaggo/swag/cmd/swag@v1.16.4
BINARY_NAME := metald

# A tag if one exists, else the short commit, with -dirty on uncommitted work.
# Falls back to "dev" in a tree with no commits. Override: make build VERSION=1.2.3
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)

# -trimpath drops build paths, -s -w drop the symbol table and DWARF.
GO_BUILD_FLAGS := -trimpath -buildvcs=false -ldflags="-s -w -X main.version=$(VERSION)"

.PHONY: build clean openapi version

build: openapi
	mkdir -p $(OUTPUT_DIRECTORY)
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build $(GO_BUILD_FLAGS) -o $(OUTPUT_DIRECTORY)/$(BINARY_NAME)-linux-amd64 ./cmd/metald
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build $(GO_BUILD_FLAGS) -o $(OUTPUT_DIRECTORY)/$(BINARY_NAME)-linux-arm64 ./cmd/metald

openapi:
	$(SWAG) init -g main.go --dir ./cmd/metald,./$(SPEC_DIRECTORY) --parseInternal --outputTypes json -o $(SPEC_DIRECTORY)

version:
	@echo $(VERSION)

clean:
	rm -rf $(OUTPUT_DIRECTORY)
