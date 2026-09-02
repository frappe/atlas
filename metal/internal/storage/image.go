package storage

// Image operations: promote a VM snapshot into a standalone warm image, list and
// delete images, and locate an image's warm memory files.

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/frappe/atlas/metal/internal/hostcmd"
)

// imageMemDir is where an image's warm memory files live. It sits on the same
// filesystem as the jailer chroot, so staging a mem file into a VM is a hard link.
func (z *ZFS) imageMemDir(ref string) string { return filepath.Join(z.imagesDir, ref) }

// ImageMemory returns an image's state and memory file paths and whether the
// image is warm (its memory file exists).
func (z *ZFS) ImageMemory(ref string) (state, mem string, warm bool) {
	dir := z.imageMemDir(ref)
	state = filepath.Join(dir, "state")
	mem = filepath.Join(dir, "mem")
	_, err := os.Stat(mem)
	return state, mem, err == nil
}

// Promote builds a standalone warm image from a VM's snapshot. The disk is a full
// independent copy (zfs send | zfs receive), so the image shares no lineage with
// the source VM; the kernel and the memory files are copied into the image store.
func (z *ZFS) Promote(ctx context.Context, r PromoteRequest) error {
	base := z.baseDataset(r.Ref)
	if datasetExists(ctx, base) {
		return ErrInUse // the image ref is already taken
	}
	if err := zfsSendRecv(ctx, z.snap(r.SrcVMID, r.SnapName), base); err != nil {
		return notFoundAware(err)
	}
	// The received dataset carries the source snapshot's name. Drop it and mark the
	// image @ready, which is what new VMs clone from.
	_ = hostcmd.Run(ctx, "zfs", "destroy", base+"@"+r.SnapName)
	if err := hostcmd.Run(ctx, "zfs", "snapshot", z.baseSnapshot(r.Ref)); err != nil {
		return err
	}
	// Copy the kernel so VMs from this image can also cold-boot after a later stop.
	if err := hostcmd.Run(ctx, "cp", "-a", "--reflink=auto",
		filepath.Join(z.kernelDir, r.SrcRef), filepath.Join(z.kernelDir, r.Ref)); err != nil {
		return err
	}
	// Copy the memory files into the image store as independent files (reflink when
	// the filesystem supports it, else a full copy).
	dir := z.imageMemDir(r.Ref)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	state, mem := filepath.Join(dir, "state"), filepath.Join(dir, "mem")
	if err := hostcmd.Run(ctx, "cp", "--reflink=auto", r.StateFile, state); err != nil {
		return err
	}
	if err := hostcmd.Run(ctx, "cp", "--reflink=auto", r.MemFile, mem); err != nil {
		return err
	}
	// 0644 so any VM's uid can read the files when they are hard-linked into a
	// chroot on warm start.
	_ = os.Chmod(state, 0o644)
	_ = os.Chmod(mem, 0o644)
	return nil
}

// Images lists the pool's images and whether each carries a memory capture.
func (z *ZFS) Images(ctx context.Context) ([]ImageInfo, error) {
	// zfs list -Hp -t volume -o name,volsize,creation -r <pool>/images: the image
	// zvols, with exact size (bytes) and creation (Unix epoch).
	out, err := hostcmd.Output(ctx, "zfs", "list", "-Hp", "-t", "volume", "-o", "name,volsize,creation", "-r", z.imagesDataset())
	if err != nil {
		if notFoundAware(err) == ErrNotFound {
			return nil, nil // no images dataset yet, so no images
		}
		return nil, err
	}
	prefix := z.imagesDataset() + "/"
	var imgs []ImageInfo
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		f := strings.Fields(line)
		if len(f) != 3 {
			continue
		}
		ref, ok := strings.CutPrefix(f[0], prefix)
		if !ok || strings.Contains(ref, "/") {
			continue
		}
		size, _ := strconv.ParseInt(f[1], 10, 64)
		created, _ := strconv.ParseInt(f[2], 10, 64)
		_, _, warm := z.ImageMemory(ref)
		imgs = append(imgs, ImageInfo{
			Ref: ref, Warm: warm, SizeMiB: int(size >> 20), CreatedAt: time.Unix(created, 0),
		})
	}
	return imgs, nil
}

// DeleteImage removes an image's disk, kernel, and memory files. ErrInUse if any
// VM cloned from it still exists; ErrNotFound if the image is unknown.
func (z *ZFS) DeleteImage(ctx context.Context, ref string) error {
	// zfs destroy -r <base>: destroy the image zvol and its @ready snapshot. It
	// fails while a clone (a VM disk) still depends on @ready.
	err := hostcmd.Run(ctx, "zfs", "destroy", "-r", z.baseDataset(ref))
	switch {
	case err == nil:
	case strings.Contains(err.Error(), "does not exist"):
		return ErrNotFound
	case strings.Contains(err.Error(), "dependent clone"):
		return ErrInUse
	default:
		return err
	}
	_ = os.RemoveAll(z.imageMemDir(ref))
	_ = os.RemoveAll(filepath.Join(z.kernelDir, ref))
	return nil
}

// zfsSendRecv streams a snapshot into a new dataset: zfs send <snap> | zfs recv
// <dst>. It is a full, independent copy, so dst shares no blocks with the source.
func zfsSendRecv(ctx context.Context, snap, dst string) error {
	send := exec.CommandContext(ctx, "zfs", "send", snap)
	recv := exec.CommandContext(ctx, "zfs", "recv", dst)
	pipe, err := send.StdoutPipe()
	if err != nil {
		return err
	}
	recv.Stdin = pipe
	var sendErr, recvErr strings.Builder
	send.Stderr, recv.Stderr = &sendErr, &recvErr
	if err := recv.Start(); err != nil {
		return err
	}
	if err := send.Run(); err != nil {
		_ = recv.Wait()
		return fmt.Errorf("zfs send: %w: %s", err, strings.TrimSpace(sendErr.String()))
	}
	if err := recv.Wait(); err != nil {
		return fmt.Errorf("zfs recv: %w: %s", err, strings.TrimSpace(recvErr.String()))
	}
	return nil
}
