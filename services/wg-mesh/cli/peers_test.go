package main

import (
	"encoding/json"
	"net/netip"
	"testing"
)

func TestDecodePeerStatePrefersThePrivateAddress(t *testing.T) {
	contents, err := json.Marshal([]peerState{
		{MeshAddress: "fdab:1::2", PublicAddress: "192.0.2.2", PrivateAddress: "10.0.0.2", MAC: "02:00:00:00:00:02"},
		{MeshAddress: "fdab:1::3", PublicAddress: "192.0.2.3", MAC: "02:00:00:00:00:03"},
	})
	if err != nil {
		t.Fatal(err)
	}

	peers, err := decodePeerState(contents)
	if err != nil {
		t.Fatal(err)
	}
	if got := netip.AddrFrom4(peers[0].IPv4).String(); got != "10.0.0.2" {
		t.Fatalf("peer with a private address = %s", got)
	}
	if got := netip.AddrFrom4(peers[1].IPv4).String(); got != "192.0.2.3" {
		t.Fatalf("peer without a private address = %s", got)
	}
}

func TestDecodePeerStateRejectsInvalidOrRepeatedPeers(t *testing.T) {
	valid := peerState{MeshAddress: "fdab:1::2", PublicAddress: "192.0.2.2", MAC: "02:00:00:00:00:02"}
	cases := [][]peerState{
		{{MeshAddress: "fdaa:1::2", PublicAddress: "192.0.2.2", MAC: "02:00:00:00:00:02"}},
		{{MeshAddress: "fdab:1::2", PublicAddress: "not-an-address", MAC: "02:00:00:00:00:02"}},
		{{MeshAddress: "fdab:1::2", PublicAddress: "192.0.2.2", MAC: "ff:ff:ff:ff:ff:ff"}},
		{valid, valid},
	}
	for _, entries := range cases {
		contents, err := json.Marshal(entries)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := decodePeerState(contents); err == nil {
			t.Errorf("accepted invalid peers: %+v", entries)
		}
	}
}
