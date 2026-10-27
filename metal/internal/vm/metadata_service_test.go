package vm

import (
	"errors"
	"strings"
	"testing"
	"time"
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

func TestSetMetadataHostTimeWritesTheKeyTheGuestAgentReads(t *testing.T) {
	document := Specification{}.MetadataServiceData("vm-1", "", "")
	SetMetadataHostTime(document, time.UnixMilli(1792800000123))

	metadata := document["latest"].(map[string]any)["meta-data"].(map[string]any)
	if value := metadata["host-time-unix-ms"]; value != "1792800000123" {
		t.Fatalf("host-time-unix-ms = %v", value)
	}
}
