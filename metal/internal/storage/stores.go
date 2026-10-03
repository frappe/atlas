// Package storage manages ZFS images, virtual machine disks, and snapshots.
package storage

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"path/filepath"
	"strings"
	"sync"

	"github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/vm"
)

var (
	// ErrNotFound indicates that a disk, snapshot, or image does not exist.
	ErrNotFound = errors.New("storage: not found")

	// ErrInUse indicates that a host artifact has dependent virtual machines.
	ErrInUse = vm.ErrInUse

	// ErrImageConflict indicates that an image reference has different content.
	ErrImageConflict = errors.New("storage: image content conflict")

	// ErrImageIntegrity indicates that image verification failed.
	ErrImageIntegrity = errors.New("storage: image integrity check failed")

	// ErrInvalidUpload indicates that upload parts do not match the artifact.
	// The caller sent a bad request, so it must not read as a host fault.
	ErrInvalidUpload = errors.New("storage: invalid upload parts")

	// ErrShuttingDown indicates that the store no longer accepts new work. The
	// caller should try again against the restarted daemon.
	ErrShuttingDown = errors.New("storage: shutting down")

	// ErrDiskNotFresh reports an existing VM disk that is not an unchanged clone of
	// the requested source snapshot. Warm memory must not resume over it.
	ErrDiskNotFresh = errors.New("storage: disk is not an unchanged clone")
)

// ZFSPool manages datasets in one ZFS pool.
type ZFSPool struct {
	name string
}

// VirtualMachineStore manages virtual machine disks.
type VirtualMachineStore struct {
	pool   *ZFSPool
	images *ImageStore
}

// ImageStore manages local image artifacts and cache policy.
type ImageStore struct {
	pool         *ZFSPool
	directory    string
	policiesFile string
	httpClient   *http.Client
	imageLocks   sync.Map
	logger       *slog.Logger
}

// SnapshotStore manages staged image snapshots.
type SnapshotStore struct {
	pool          *ZFSPool
	images        *ImageStore
	directory     string
	httpClient    *http.Client
	snapshotLocks sync.Map

	uploadsMutex     sync.Mutex
	uploads          map[string]*snapshotUpload
	lifecycleMutex   sync.Mutex
	rootContext      context.Context
	rootCancel       context.CancelFunc
	uploadsWaitGroup sync.WaitGroup
	closed           bool
	logger           *slog.Logger
	runner           stagingCommandRunner
}

// stagingCommandRunner runs the ZFS commands used for staging cleanup.
type stagingCommandRunner interface {
	Run(ctx context.Context, name string, args ...string) error
	Output(ctx context.Context, name string, args ...string) (string, error)
}

// Stores contains the host storage services. They share one pool and one image
// store, so a disk clone and its base image always agree.
type Stores struct {
	Pool            *ZFSPool
	VirtualMachines *VirtualMachineStore
	Images          *ImageStore
	Snapshots       *SnapshotStore
}

// NewStores returns storage services for one ZFS pool.
func NewStores(parentContext context.Context, poolName, imagesDirectory string, logger *slog.Logger) Stores {
	rootContext, rootCancel := context.WithCancel(parentContext)
	if logger == nil {
		logger = slog.Default()
	}
	pool := &ZFSPool{name: poolName}
	baseDirectory := filepath.Dir(imagesDirectory)
	images := &ImageStore{
		pool:         pool,
		directory:    imagesDirectory,
		policiesFile: filepath.Join(baseDirectory, "image-policies.json"),
		httpClient:   newImageHTTPClient(),
		logger:       logger,
	}

	return Stores{
		Pool:            pool,
		VirtualMachines: &VirtualMachineStore{pool: pool, images: images},
		Images:          images,
		Snapshots: &SnapshotStore{
			pool:        pool,
			images:      images,
			directory:   filepath.Join(baseDirectory, "snapshots"),
			httpClient:  newImageHTTPClient(),
			uploads:     make(map[string]*snapshotUpload),
			rootContext: rootContext,
			rootCancel:  rootCancel,
			logger:      logger,
			runner:      platform.HostRunner{},
		},
	}
}

// Shutdown stops new uploads and waits for active uploads to finish.
func (store *SnapshotStore) Shutdown(shutdownContext context.Context) error {
	store.lifecycleMutex.Lock()
	if !store.closed {
		store.closed = true
		store.rootCancel()
	}
	store.lifecycleMutex.Unlock()

	finished := make(chan struct{})
	go func() {
		store.uploadsWaitGroup.Wait()
		close(finished)
	}()
	select {
	case <-finished:
		return nil
	case <-shutdownContext.Done():
		return fmt.Errorf("wait for snapshot uploads: %w", shutdownContext.Err())
	}
}

// imageDirectory holds the kernel, manifest, and warm artifacts of one image.
func (store *ImageStore) imageDirectory(imageReference string) string {
	return filepath.Join(store.directory, imageReference)
}

// kernelFile is the uncompressed kernel of one image.
func (store *ImageStore) kernelFile(imageReference string) string {
	return filepath.Join(store.imageDirectory(imageReference), "vmlinux")
}

// manifestFile records the content one image reference is bound to.
func (store *ImageStore) manifestFile(imageReference string) string {
	return filepath.Join(store.imageDirectory(imageReference), "manifest.json")
}

// imageLock returns the lock that serializes work on one image reference.
func (store *ImageStore) imageLock(imageReference string) *sync.Mutex {
	lock, _ := store.imageLocks.LoadOrStore(imageReference, &sync.Mutex{})
	return lock.(*sync.Mutex)
}

// snapshotLock serializes staging, upload, and delete of one snapshot.
func (store *SnapshotStore) snapshotLock(snapshotID string) *sync.Mutex {
	lock, _ := store.snapshotLocks.LoadOrStore(snapshotID, &sync.Mutex{})
	return lock.(*sync.Mutex)
}

// snapshotDirectory holds the staged kernel and metadata of one snapshot.
func (store *SnapshotStore) snapshotDirectory(snapshotID string) string {
	return filepath.Join(store.directory, snapshotID)
}

// notFoundAware turns the ZFS "does not exist" message into ErrNotFound. ZFS
// reports a missing dataset on stderr, not with a distinct exit code.
func notFoundAware(err error) error {
	if err != nil && strings.Contains(err.Error(), "does not exist") {
		return ErrNotFound
	}

	return err
}

// VirtualMachineStorageRequest identifies the files and disk for one virtual machine.
type VirtualMachineStorageRequest struct {
	VirtualMachineID string
	ImageReference   string
	Image            vm.Image
	ChrootRoot       string
	UserID           uint32
	GroupID          uint32
	DiskMiB          int
	SourceSnapshot   string
}

// VirtualMachineRescueBootRequest identifies the rescue image and the virtual
// machine whose own disk attaches alongside it.
type VirtualMachineRescueBootRequest struct {
	RescueGeneration     uint64
	VirtualMachineID     string
	RescueImageReference string
	RescueImage          vm.Image
	ChrootRoot           string
	UserID               uint32
	GroupID              uint32
}

// BootConfiguration contains the files that Firecracker needs to boot.
type BootConfiguration struct {
	Kernel     string
	KernelArgs string
	Drives     []Drive
}

// Drive describes one Firecracker block device.
type Drive struct {
	Path     string
	ReadOnly bool
	Root     bool
}

// Usage describes disk allocation.
type Usage = vm.DiskUsage
