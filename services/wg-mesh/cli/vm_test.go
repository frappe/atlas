package main

import "testing"

func TestParseVMStateBuildsPrefixesAndRoutes(t *testing.T) {
	state, err := parseVMState(
		"fdaa:1:0:2::5",
		[]string{"2001:db8:100::/64"},
		[]string{"::/0=fdaa:1:0:2::1", "2001:db8:200::/64=fdaa:1:0:2::2"},
	)
	if err != nil {
		t.Fatal(err)
	}
	if len(state.prefixes) != 1 || len(state.routes) != 2 {
		t.Fatalf("state has %d prefixes and %d routes", len(state.prefixes), len(state.routes))
	}
}

func TestParseVMStateRejectsBadInput(t *testing.T) {
	cases := []struct {
		address  string
		prefixes []string
		routes   []string
	}{
		{address: "not-an-address"},
		{address: "fdaa:1::1", prefixes: []string{"2001:db8::1/64"}},
		{address: "fdaa:1::1", routes: []string{"::/0"}},
		{address: "fdaa:1::1", routes: []string{"::/0=2001:db8::1"}},
		{address: "fdaa:1::1", routes: []string{"::/0=fdaa:1::2", "::/0=fdaa:1::3"}},
	}
	for _, test := range cases {
		if _, err := parseVMState(test.address, test.prefixes, test.routes); err == nil {
			t.Errorf("accepted address %q, prefixes %v, routes %v", test.address, test.prefixes, test.routes)
		}
	}
}

func TestParseControllerAcceptsOnlyATenantZeroMeshAddress(t *testing.T) {
	if address, err := parseController(""); err != nil || address != [16]byte{} {
		t.Fatalf("an empty controller must clear the map, got %v, %v", address, err)
	}
	if _, err := parseController("fdaa:1::ffff:ffff:ffff:ffff"); err != nil {
		t.Fatal(err)
	}
	for _, text := range []string{"fdab:1::7", "fdaa:1:0:2::1", "10.1.0.2"} {
		if _, err := parseController(text); err == nil {
			t.Errorf("accepted controller %q", text)
		}
	}
}
