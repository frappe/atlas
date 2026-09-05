package vm

import "testing"

func TestEgressCapabilities(t *testing.T) {
	for _, testCase := range []struct {
		egress          Egress
		virtualEthernet bool
		internetPath    bool
	}{
		{EgressUplink, true, true},
		{EgressMesh, true, false},
		{EgressNone, false, false},
	} {
		if got := testCase.egress.HasVirtualEthernet(); got != testCase.virtualEthernet {
			t.Fatalf("egress %q veth = %v, want %v", testCase.egress, got, testCase.virtualEthernet)
		}
		if got := testCase.egress.HasInternetPath(); got != testCase.internetPath {
			t.Fatalf("egress %q internet = %v, want %v", testCase.egress, got, testCase.internetPath)
		}
	}
}

func TestEgressIsValidRejectsUnknownModes(t *testing.T) {
	for _, egress := range []Egress{EgressUplink, EgressMesh, EgressNone} {
		if !egress.IsValid() {
			t.Fatalf("egress %q must be valid", egress)
		}
	}
	for _, egress := range []Egress{"", "host", "server"} {
		if egress.IsValid() {
			t.Fatalf("egress %q must not be valid", egress)
		}
	}
}
