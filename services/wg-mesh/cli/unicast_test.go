package main

import (
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"testing"
)

func writeTestPeerFile(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "peers")
	if err := os.WriteFile(path, []byte(contents), 0644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestReadUnicastPeerEntries(t *testing.T) {
	path := writeTestPeerFile(t, ""+"# participating hosts\n"+"10.20.0.11\n"+"\n"+"10.20.0.12 # inline comment\n"+"10.20.0.11\n")

	entries, err := readUnicastPeerEntries(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 {
		t.Fatalf("got %d entries, want 2", len(entries))
	}
	if entries[0].String() != "10.20.0.11" || entries[1].String() != "10.20.0.12" {
		t.Fatalf("got entries %v", entries)
	}
}

func TestReadUnicastPeerEntriesRejectsNonIPv4(t *testing.T) {
	path := writeTestPeerFile(t, "10.20.0.11\nfdab::1\n")

	if _, err := readUnicastPeerEntries(path); err == nil {
		t.Fatal("expected an error for a non-IPv4 entry")
	}
}

func TestReadUnicastPeerEntriesRejectsOverflow(t *testing.T) {
	contents := ""
	for index := 0; index <= unicastPeerLimit; index++ {
		address := netip.AddrFrom4([4]byte{10, byte(index >> 8), byte(index), 1})
		contents += address.String() + "\n"
	}
	path := writeTestPeerFile(t, contents)

	if _, err := readUnicastPeerEntries(path); err == nil {
		t.Fatal("expected an error for more peers than the map holds")
	}
}

func TestUpdateUnicastPeerFile(t *testing.T) {
	path := writeTestPeerFile(t, "10.20.0.11\n")

	if err := updateUnicastPeerFile(path, "10.20.0.12", true); err != nil {
		t.Fatal(err)
	}
	if err := updateUnicastPeerFile(path, "10.20.0.11", true); err != nil {
		t.Fatal(err)
	}
	entries, err := readUnicastPeerEntries(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 {
		t.Fatalf("got %d entries after add, want 2", len(entries))
	}

	if err := updateUnicastPeerFile(path, "10.20.0.11", false); err != nil {
		t.Fatal(err)
	}
	if err := updateUnicastPeerFile(path, "10.20.0.11", false); err != nil {
		t.Fatal(err)
	}
	entries, err = readUnicastPeerEntries(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].String() != "10.20.0.12" {
		t.Fatalf("got entries %v after remove, want 10.20.0.12", entries)
	}
}

func TestUpdateUnicastPeerFileCreatesMissingFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "peers")

	if err := updateUnicastPeerFile(path, "10.20.0.11", true); err != nil {
		t.Fatal(err)
	}
	entries, err := readUnicastPeerEntries(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].String() != "10.20.0.11" {
		t.Fatalf("got entries %v, want 10.20.0.11", entries)
	}
}

func TestUpdateUnicastPeerFileRejectsNonIPv4(t *testing.T) {
	path := writeTestPeerFile(t, "10.20.0.11\n")

	if err := updateUnicastPeerFile(path, "fdab::1", true); err == nil {
		t.Fatal("expected an error for a non-IPv4 address")
	}
}

func TestUnicastTransportPeersDropsSelf(t *testing.T) {
	self := [4]byte{10, 20, 0, 10}
	entries := []netip.Addr{
		netip.MustParseAddr("10.20.0.10"),
		netip.MustParseAddr("10.20.0.11"),
	}

	peers := unicastTransportPeers(entries, self)
	if len(peers) != 1 || peers[0].String() != "10.20.0.11" {
		t.Fatalf("got peers %v, want 10.20.0.11", peers)
	}
}

func TestUnicastPeerMapValuesPacksDensely(t *testing.T) {
	peers := []netip.Addr{
		netip.MustParseAddr("10.20.0.11"),
		netip.MustParseAddr("10.20.0.12"),
	}

	values := unicastPeerMapValues(peers)
	if len(values) != unicastPeerLimit {
		t.Fatalf("got %d values, want %d", len(values), unicastPeerLimit)
	}
	if values[0] != [4]byte{10, 20, 0, 11} {
		t.Fatalf("got value %v at index 0", values[0])
	}
	if values[1] != [4]byte{10, 20, 0, 12} {
		t.Fatalf("got value %v at index 1", values[1])
	}
	if !slices.Equal(values[2:], make([][4]byte, unicastPeerLimit-2)) {
		t.Fatal("the slots after the peers must hold zero")
	}
}
