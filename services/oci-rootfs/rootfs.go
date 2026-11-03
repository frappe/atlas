package main

import (
	"archive/tar"
	"compress/gzip"
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/klauspost/compress/zstd"
	"golang.org/x/sys/unix"
)

const fileSystemBlockBytes = 4096

//go:embed dist/guest-init
var guestInitBinary []byte

// buildRootfs writes the image layers, guest-init, and the image configuration into one ext4 file.
func buildRootfs(ctx context.Context, reference, output string) error {
	registry, manifestReference, err := parseReference(reference)
	if err != nil {
		return err
	}
	image, err := registry.fetchImageManifest(ctx, manifestReference)
	if err != nil {
		return err
	}

	directory, err := os.MkdirTemp(filepath.Dir(output), ".oci-rootfs-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(directory)
	root, err := os.OpenRoot(directory)
	if err != nil {
		return err
	}
	defer root.Close()

	for _, layer := range image.Layers {
		if err := registry.applyLayer(ctx, layer, root); err != nil {
			return fmt.Errorf("apply layer %s: %w", layer.Digest, err)
		}
	}
	if err := registry.installGuestInit(ctx, image.Config.Digest, root); err != nil {
		return fmt.Errorf("install guest init: %w", err)
	}
	return writeFileSystem(ctx, directory, output)
}

// writeFileSystem makes the smallest practical ext4 file. The guest grows it to
// the VM disk at boot. It counts apparent sizes, because the file system of the
// build machine can compress what st_blocks reports.
func writeFileSystem(ctx context.Context, directory, output string) error {
	var sizeBytes, entries int64
	err := filepath.WalkDir(directory, func(_ string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		information, err := entry.Info()
		if err != nil {
			return err
		}
		sizeBytes += (information.Size()/fileSystemBlockBytes + 1) * fileSystemBlockBytes
		entries++
		return nil
	})
	if err != nil {
		return err
	}

	file, err := os.Create(output)
	if err != nil {
		return err
	}
	defer file.Close()
	// The margin holds ext4 metadata and the journal.
	if err := file.Truncate(sizeBytes + sizeBytes/8 + 32<<20); err != nil {
		return err
	}
	command := exec.CommandContext(ctx, "mkfs.ext4", "-q", "-F", "-b", strconv.Itoa(fileSystemBlockBytes), "-I", "256",
		"-N", strconv.FormatInt(entries+entries/4+1024, 10),
		// Remount read-only at the first file system error, as in the Ubuntu images.
		"-e", "remount-ro", "-d", directory, output)
	command.Stderr = os.Stderr
	return command.Run()
}

// applyLayer unpacks one layer and verifies its digest over the compressed bytes.
func (registry *registryClient) applyLayer(ctx context.Context, layer descriptor, root *os.Root) error {
	body, err := registry.get(ctx, "blobs/"+layer.Digest)
	if err != nil {
		return err
	}
	defer body.Close()

	digest := sha256.New()
	archive, closeArchive, err := openLayerArchive(io.TeeReader(body, digest), layer.MediaType)
	if err != nil {
		return err
	}
	defer closeArchive()
	if err := unpackLayer(archive, root); err != nil {
		return err
	}

	// A tar reader can leave the end of the stream unread.
	if _, err := io.Copy(digest, body); err != nil {
		return err
	}
	if "sha256:"+hex.EncodeToString(digest.Sum(nil)) != layer.Digest {
		return errors.New("digest mismatch")
	}
	return nil
}

func openLayerArchive(source io.Reader, mediaType string) (io.Reader, func(), error) {
	switch {
	case strings.HasSuffix(mediaType, "zstd"):
		// Concurrency 1 keeps reads on this goroutine, so the digest can continue after the archive ends.
		decoder, err := zstd.NewReader(source, zstd.WithDecoderConcurrency(1))
		if err != nil {
			return nil, nil, err
		}
		return decoder, decoder.Close, nil
	case strings.HasSuffix(mediaType, "gzip"):
		decoder, err := gzip.NewReader(source)
		if err != nil {
			return nil, nil, err
		}
		return decoder, func() { decoder.Close() }, nil
	default:
		return source, func() {}, nil
	}
}

// unpackLayer applies one layer. A whiteout hides lower layers only, and an
// archive can list it after the new entries of its directory. os.Root rejects
// every path that leaves the directory, because the archive is untrusted.
func unpackLayer(archive io.Reader, root *os.Root) error {
	reader := tar.NewReader(archive)
	written := map[string]bool{}
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		name, err := resolveInImage(root, header.Name)
		if err != nil || name == "" || name == "dev" || strings.HasPrefix(name, "dev/") {
			continue
		}

		parent, base := path.Split(name)
		if base == ".wh..wh..opq" {
			if err := removeLowerEntries(root, path.Clean(parent), written, true); err != nil {
				return err
			}
			continue
		}
		if hidden, isWhiteout := strings.CutPrefix(base, ".wh."); isWhiteout {
			if err := removeLowerEntries(root, path.Join(parent, hidden), written, false); err != nil {
				return err
			}
			continue
		}

		if err := root.MkdirAll(path.Clean(parent), 0o755); err != nil {
			return err
		}
		if err := writeEntry(root, name, header, reader); err != nil {
			return fmt.Errorf("%s: %w", name, err)
		}
		for current := name; current != "."; current = path.Dir(current) {
			written[current] = true
		}
	}
}

// removeLowerEntries removes the entries at name that this layer did not write.
// With onlyChildren, it keeps name itself, as an opaque whiteout does.
func removeLowerEntries(root *os.Root, name string, written map[string]bool, onlyChildren bool) error {
	if !written[name] && !onlyChildren {
		return root.RemoveAll(name)
	}
	if information, err := root.Lstat(name); err != nil || !information.IsDir() {
		return nil
	}
	directory, err := root.Open(name)
	if err != nil {
		return err
	}
	children, err := directory.Readdirnames(-1)
	directory.Close()
	if err != nil {
		return err
	}
	for _, child := range children {
		if err := removeLowerEntries(root, path.Join(name, child), written, false); err != nil {
			return err
		}
	}
	return nil
}

// resolveInImage returns the path of name in the image with the symlinks in its
// parent directories followed, as the guest sees them with / as the image root.
// os.Root refuses absolute targets such as var/run -> /run.
func resolveInImage(root *os.Root, name string) (string, error) {
	parent, base := path.Split(path.Clean("/" + name))
	pending := strings.Split(parent, "/")
	resolved := "."
	for links := 0; len(pending) > 0; {
		component := pending[0]
		pending = pending[1:]
		switch component {
		case "", ".":
			continue
		case "..":
			resolved = path.Dir(resolved)
			continue
		}
		candidate := path.Join(resolved, component)
		target, err := root.Readlink(candidate)
		if err != nil {
			resolved = candidate
			continue
		}
		if links++; links > 40 {
			return "", fmt.Errorf("%s: too many symlinks", name)
		}
		if path.IsAbs(target) {
			resolved = "."
		}
		pending = append(strings.Split(target, "/"), pending...)
	}
	if base == "" {
		return strings.TrimPrefix(resolved, "."), nil
	}
	return path.Join(resolved, base), nil
}

func writeEntry(root *os.Root, name string, header *tar.Header, content io.Reader) error {
	switch header.Typeflag {
	case tar.TypeDir:
		if information, err := root.Lstat(name); err == nil && !information.IsDir() {
			if err := root.Remove(name); err != nil {
				return err
			}
		}
		if err := root.Mkdir(name, 0o755); err != nil && !errors.Is(err, fs.ErrExist) {
			return err
		}
		if err := root.Lchown(name, header.Uid, header.Gid); err != nil {
			return err
		}
		return root.Chmod(name, header.FileInfo().Mode())
	case tar.TypeReg:
		return writeRegularFile(root, name, header, content)
	case tar.TypeSymlink:
		if err := root.RemoveAll(name); err != nil {
			return err
		}
		if err := root.Symlink(header.Linkname, name); err != nil {
			return err
		}
		return root.Lchown(name, header.Uid, header.Gid)
	case tar.TypeLink:
		if err := root.RemoveAll(name); err != nil {
			return err
		}
		target, err := resolveInImage(root, header.Linkname)
		if err != nil {
			return err
		}
		return root.Link(target, name)
	default:
		// Device nodes and pipes are not image content that a VM needs. devtmpfs provides /dev.
		return nil
	}
}

func writeRegularFile(root *os.Root, name string, header *tar.Header, content io.Reader) error {
	if err := root.RemoveAll(name); err != nil {
		return err
	}
	file, err := root.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer file.Close()
	if _, err := io.Copy(file, content); err != nil {
		return err
	}
	if err := file.Chown(header.Uid, header.Gid); err != nil {
		return err
	}
	if err := file.Chmod(header.FileInfo().Mode()); err != nil {
		return err
	}
	// After chown, which clears file capabilities.
	for key, value := range header.PAXRecords {
		if attribute, found := strings.CutPrefix(key, "SCHILY.xattr."); found {
			if err := unix.Fsetxattr(int(file.Fd()), attribute, []byte(value), 0); err != nil {
				return fmt.Errorf("set %s: %w", attribute, err)
			}
		}
	}
	return root.Chtimes(name, time.Time{}, header.ModTime)
}

// installGuestInit makes guest-init the first process and saves the image configuration for it.
func (registry *registryClient) installGuestInit(ctx context.Context, configurationDigest string, root *os.Root) error {
	data, err := registry.read(ctx, "blobs/"+configurationDigest, configurationDigest)
	if err != nil {
		return err
	}
	var image struct {
		Config json.RawMessage `json:"config"`
	}
	if err := json.Unmarshal(data, &image); err != nil {
		return err
	}

	if err := root.MkdirAll(".atlas", 0o755); err != nil {
		return err
	}
	if err := root.WriteFile(".atlas/image.json", image.Config, 0o644); err != nil {
		return err
	}

	// On Alpine, /sbin/init is a symlink to BusyBox, which must stay intact.
	initPath, err := resolveInImage(root, "sbin/init")
	if err != nil {
		return err
	}
	if err := root.MkdirAll(path.Dir(initPath), 0o755); err != nil {
		return err
	}
	if err := root.RemoveAll(initPath); err != nil {
		return err
	}
	return root.WriteFile(initPath, guestInitBinary, 0o755)
}
