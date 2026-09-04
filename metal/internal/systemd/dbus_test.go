package systemd

import (
	"errors"
	"fmt"
	"testing"

	godbus "github.com/godbus/dbus/v5"
)

func TestIsUnitNotLoaded(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want bool
	}{
		{"nil", nil, false},
		{"dbus no such unit", godbus.Error{Name: errorNoSuchUnit}, true},
		{"wrapped dbus no such unit", fmt.Errorf("reset VM unit: %w", godbus.Error{Name: errorNoSuchUnit}), true},
		{"message substring", errors.New("Unit metal-vm@VM-00015.service not loaded."), true},
		{"unrelated dbus error", godbus.Error{Name: "org.freedesktop.systemd1.JobFailed"}, false},
		{"unrelated error", errors.New("connection refused"), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := isUnitNotLoaded(tc.err); got != tc.want {
				t.Fatalf("isUnitNotLoaded(%v) = %v, want %v", tc.err, got, tc.want)
			}
		})
	}
}
