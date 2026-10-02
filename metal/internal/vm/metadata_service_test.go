package vm

import (
	"errors"
	"strings"
	"testing"
)

func TestMetadataServiceSizeRejectsAnOversizedDocument(t *testing.T) {
	specification := Specification{Metadata: map[string]string{
		"key": strings.Repeat("v", MetadataServiceSizeLimitBytes-metadataServiceReserveBytes-200),
	}}
	if err := specification.validateMetadataServiceSize("vm-1"); err != nil {
		t.Fatal(err)
	}

	specification.Metadata["key"] += strings.Repeat("v", 200)
	if err := specification.validateMetadataServiceSize("vm-1"); !errors.Is(err, ErrMetadataServiceTooLarge) {
		t.Fatalf("error = %v", err)
	}
}
