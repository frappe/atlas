package storage

import (
	"net/http"
	"path/filepath"
	"strings"
	"sync"
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
}

// SnapshotStore manages staged image snapshots.
type SnapshotStore struct {
	pool          *ZFSPool
	images        *ImageStore
	directory     string
	httpClient    *http.Client
	snapshotLocks sync.Map

	uploadsMutex sync.Mutex
	uploads      map[string]*snapshotUpload
}

// Stores contains the host storage services.
type Stores struct {
	Pool            *ZFSPool
	VirtualMachines *VirtualMachineStore
	Images          *ImageStore
	Snapshots       *SnapshotStore
}

// NewStores returns storage services for one ZFS pool.
func NewStores(poolName, imagesDirectory string) Stores {
	pool := &ZFSPool{name: poolName}
	baseDirectory := filepath.Dir(imagesDirectory)
	images := &ImageStore{
		pool:         pool,
		directory:    imagesDirectory,
		policiesFile: filepath.Join(baseDirectory, "image-policies.json"),
		httpClient:   newImageHTTPClient(),
	}

	return Stores{
		Pool:            pool,
		VirtualMachines: &VirtualMachineStore{pool: pool, images: images},
		Images:          images,
		Snapshots: &SnapshotStore{
			pool:       pool,
			images:     images,
			directory:  filepath.Join(baseDirectory, "snapshots"),
			httpClient: newImageHTTPClient(),
			uploads:    make(map[string]*snapshotUpload),
		},
	}
}

func (store *ImageStore) imageDirectory(imageReference string) string {
	return filepath.Join(store.directory, imageReference)
}

func (store *ImageStore) kernelFile(imageReference string) string {
	return filepath.Join(store.imageDirectory(imageReference), "vmlinux")
}

func (store *ImageStore) manifestFile(imageReference string) string {
	return filepath.Join(store.imageDirectory(imageReference), "manifest.json")
}

func (store *ImageStore) imageLock(imageReference string) *sync.Mutex {
	lock, _ := store.imageLocks.LoadOrStore(imageReference, &sync.Mutex{})
	return lock.(*sync.Mutex)
}

func (store *SnapshotStore) snapshotLock(snapshotID string) *sync.Mutex {
	lock, _ := store.snapshotLocks.LoadOrStore(snapshotID, &sync.Mutex{})
	return lock.(*sync.Mutex)
}

func (pool *ZFSPool) imagesDataset() string { return pool.name + "/images" }

func (pool *ZFSPool) baseDataset(imageReference string) string {
	return pool.imagesDataset() + "/" + imageReference
}

func (pool *ZFSPool) baseSnapshot(imageReference string) string {
	return pool.baseDataset(imageReference) + "@ready"
}

func (pool *ZFSPool) virtualMachineDataset(virtualMachineID string) string {
	return pool.name + "/vms/" + virtualMachineID
}

func (pool *ZFSPool) virtualMachineDevicePath(virtualMachineID string) string {
	return "/dev/zvol/" + pool.virtualMachineDataset(virtualMachineID)
}

func (pool *ZFSPool) snapshot(virtualMachineID, snapshotName string) string {
	return pool.virtualMachineDataset(virtualMachineID) + "@" + snapshotName
}

func (pool *ZFSPool) stagingDataset(snapshotID string) string {
	return pool.name + "/staging/" + snapshotID
}

func (pool *ZFSPool) stagingDevicePath(snapshotID string) string {
	return "/dev/zvol/" + pool.stagingDataset(snapshotID)
}

func (store *SnapshotStore) snapshotDirectory(snapshotID string) string {
	return filepath.Join(store.directory, snapshotID)
}

func notFoundAware(err error) error {
	if err != nil && strings.Contains(err.Error(), "does not exist") {
		return ErrNotFound
	}

	return err
}
