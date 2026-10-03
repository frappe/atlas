package main

import (
	"errors"
	"testing"
)

func TestIgnoreMissingAcceptsAnAlreadyDeletedRoute(t *testing.T) {
	missingRoute := errors.New("ip -6 route del fdaa::/16 dev atlas-mesh: exit status 2: RTNETLINK answers: No such process")
	if err := ignoreMissing(missingRoute); err != nil {
		t.Fatalf("ignoreMissing kept %v", err)
	}

	permissionDenied := errors.New("ip -6 route del fdaa::/16 dev eth0: exit status 2: RTNETLINK answers: Operation not permitted")
	if err := ignoreMissing(permissionDenied); err == nil {
		t.Fatal("ignoreMissing hid a real failure")
	}
}
