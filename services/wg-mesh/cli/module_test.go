package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestModuleStampIsStable(t *testing.T) {
	first := moduleStamp("6.8.0-138-generic")
	second := moduleStamp("6.8.0-138-generic")

	if first == "" {
		t.Fatal("module stamp is empty")
	}
	if first != second {
		t.Fatal("the same sources and kernel must produce the same stamp")
	}
}

func TestModuleStampTracksKernelRelease(t *testing.T) {
	if moduleStamp("6.8.0-138-generic") == moduleStamp("6.8.0-140-generic") {
		t.Fatal("a different kernel release must produce a different stamp")
	}
}

func TestWriteKernelModuleSourcesWritesEveryFile(t *testing.T) {
	directory := t.TempDir()

	if err := writeKernelModuleSources(directory); err != nil {
		t.Fatal(err)
	}

	embeddedNames, err := kernelModuleSources.ReadDir("kernel_module")
	if err != nil {
		t.Fatal(err)
	}
	if len(embeddedNames) != 2 {
		t.Fatalf("the embed holds %d files, want the module source and its Makefile", len(embeddedNames))
	}

	for _, name := range []string{"atlas_neigh.c", "Makefile"} {
		embedded, err := kernelModuleSources.ReadFile("kernel_module/" + name)
		if err != nil {
			t.Fatalf("the embed holds no %s: %v", name, err)
		}

		written, err := os.ReadFile(filepath.Join(directory, name))
		if err != nil {
			t.Fatalf("the write produced no %s: %v", name, err)
		}

		if string(written) != string(embedded) {
			t.Fatalf("the written %s differs from the embedded source", name)
		}
	}
}
