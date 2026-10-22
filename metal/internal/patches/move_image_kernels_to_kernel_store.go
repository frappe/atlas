package patches

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

func moveImageKernelsToKernelStore(baseDirectory string) error {
	manifests, err := filepath.Glob(filepath.Join(baseDirectory, "images", "*", "manifest.json"))
	if err != nil {
		return err
	}
	kernelsDirectory := filepath.Join(baseDirectory, "kernels")
	if err := os.MkdirAll(kernelsDirectory, 0o755); err != nil {
		return err
	}

	for _, manifestPath := range manifests {
		kernel := filepath.Join(filepath.Dir(manifestPath), "vmlinux")
		if _, err := os.Stat(kernel); errors.Is(err, fs.ErrNotExist) {
			continue
		}
		data, err := os.ReadFile(manifestPath)
		if err != nil {
			return err
		}
		var manifest struct {
			KernelSHA256 string `json:"kernel_sha256"`
		}
		if err := json.Unmarshal(data, &manifest); err != nil {
			return fmt.Errorf("read %s: %w", manifestPath, err)
		}
		if manifest.KernelSHA256 == "" {
			return fmt.Errorf("%s has no kernel digest", manifestPath)
		}
		target := filepath.Join(kernelsDirectory, strings.ToLower(manifest.KernelSHA256))
		if err := os.Link(kernel, target); err != nil && !errors.Is(err, fs.ErrExist) {
			return err
		}
		if err := os.Remove(kernel); err != nil {
			return err
		}
	}
	return nil
}
