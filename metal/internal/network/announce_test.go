package network

import (
	"bytes"
	"net"
	"testing"
)

func TestGratuitousARPFrameAnnouncesTheAddressToEveryHost(t *testing.T) {
	mac := net.HardwareAddr{0xc6, 0x39, 0x48, 0xe6, 0x5f, 0x38}
	address := [4]byte{103, 48, 90, 252}

	want := []byte{
		0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xc6, 0x39, 0x48, 0xe6, 0x5f, 0x38, 0x08, 0x06,
		0, 1, 0x08, 0, 6, 4, 0, 2,
		0xc6, 0x39, 0x48, 0xe6, 0x5f, 0x38, 103, 48, 90, 252,
		0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 103, 48, 90, 252,
	}
	if got := gratuitousARPFrame(mac, address); !bytes.Equal(got, want) {
		t.Fatalf("frame = % x\nwant   % x", got, want)
	}
}

func TestNeighborAdvertisementOverridesTheCacheWithTheHostMAC(t *testing.T) {
	mac := net.HardwareAddr{0xc6, 0x39, 0x48, 0xe6, 0x5f, 0x38}
	address := [16]byte{0x20, 0x01, 0x0d, 0xb8, 15: 1}

	message := neighborAdvertisement(mac, address)

	if message[0] != 136 || message[4]&0x20 == 0 {
		t.Fatalf("message = % x, want a neighbor advertisement with the override flag", message[:8])
	}
	if !bytes.Equal(message[8:24], address[:]) || !bytes.Equal(message[26:], mac) {
		t.Fatalf("message = % x, want the target address and the MAC option", message)
	}
}
