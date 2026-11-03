OUTPUT_DIRECTORY := dist
GO_BUILD_FLAGS := -trimpath -buildvcs=false -ldflags="-s -w"
export CGO_ENABLED := 0
export GOOS := linux
export GOARCH := amd64

.PHONY: build guest-init clean

build: guest-init
	go build $(GO_BUILD_FLAGS) -o $(OUTPUT_DIRECTORY)/oci-rootfs-linux-amd64 .

# The CLI embeds this binary, so nothing in this package compiles without it.
guest-init:
	go build $(GO_BUILD_FLAGS) -o $(OUTPUT_DIRECTORY)/guest-init ./guest-init

clean:
	rm -rf $(OUTPUT_DIRECTORY)
