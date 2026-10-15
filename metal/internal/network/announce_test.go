package network

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeHostCommands puts ip and sysctl stubs first on PATH. ip reports an existing proxy entry
// when the proxy file exists, and every command is logged.
func fakeHostCommands(t *testing.T, isClaimed bool) (commandLog string) {
	t.Helper()
	directory := t.TempDir()
	commandLog = filepath.Join(directory, "commands.log")
	proxyFile := filepath.Join(directory, "proxy")
	if isClaimed {
		if err := os.WriteFile(proxyFile, []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	script := "#!/bin/sh\necho \"$0 $*\" >> " + commandLog + "\n" +
		"case \"$*\" in *\"neigh show proxy\"*) [ -e " + proxyFile + " ] && echo proxy;; esac\nexit 0\n"
	for _, name := range []string{"ip", "sysctl"} {
		if err := os.WriteFile(filepath.Join(directory, name), []byte(script), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", directory+string(os.PathListSeparator)+os.Getenv("PATH"))
	return commandLog
}

func readCommands(t *testing.T, commandLog string) string {
	t.Helper()
	data, err := os.ReadFile(commandLog)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestClaimPublicIPv4DoesNotAnnounceAnExistingClaim(t *testing.T) {
	commandLog := fakeHostCommands(t, true)
	announce := func(string, string) error {
		t.Fatal("an existing claim must not announce again")
		return nil
	}

	if err := claimPublicIPv4(context.Background(), 1, "bond0", "203.0.113.5", announce); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(readCommands(t, commandLog), "neigh replace proxy") {
		t.Fatal("the claim was not refreshed")
	}
}

func TestFailedAnnouncementLeavesNoProxyEntryForTheRetry(t *testing.T) {
	commandLog := fakeHostCommands(t, false)
	announce := func(string, string) error { return errors.New("no route to the interface") }

	if err := claimPublicIPv4(context.Background(), 1, "bond0", "203.0.113.5", announce); err == nil {
		t.Fatal("want the announcement error")
	}
	if strings.Contains(readCommands(t, commandLog), "neigh replace") {
		t.Fatal("a failed announcement installed the proxy entry, so the retry would skip it")
	}
}
