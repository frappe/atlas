package firecracker

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/hex"
	"encoding/pem"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/creack/pty"

	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/vm"
	"golang.org/x/crypto/ssh"
)

// sshConsoleUser is the guest account used by the SSH console.
const sshConsoleUser = "root"

// ConnectSSH opens an interactive SSH session to a VM guest. Authorization is a
// throwaway key pushed through MMDS and removed on close, so no key outlives the
// session and the guest image carries none.
func (runtime *Runtime) ConnectSSH(ctx context.Context, input vm.RuntimeMachine) (vm.SSHConnection, error) {
	select {
	case runtime.sshSlots <- struct{}{}:
	default:
		return nil, fmt.Errorf("ssh session limit reached")
	}
	releaseSlot := func() { <-runtime.sshSlots }

	keyPair, err := generateSSHKey()
	if err != nil {
		releaseSlot()
		return nil, err
	}
	index, err := sshConsoleKeyIndex()
	if err != nil {
		releaseSlot()
		return nil, err
	}

	client := api.New(runtime.configuration.socketPath(input.ID))
	if err := authorizeSSHConsoleKey(ctx, client, index, strings.TrimSpace(keyPair.authorizedKey)); err != nil {
		releaseSlot()
		return nil, fmt.Errorf("authorize ssh key: %w", err)
	}
	removeKey := func() {
		_ = authorizeSSHConsoleKey(context.WithoutCancel(ctx), client, index, nil)
	}

	namespace := filepath.Base(input.NetworkInterface.NetworkNamespacePath)
	session, err := startSSHSession(ctx, runtime.configuration.SocketsDir, namespace, sshConsoleUser, input.NetworkInterface.GuestIPAddress, keyPair.privatePEM)
	if err != nil {
		removeKey()
		releaseSlot()
		return nil, err
	}
	return &sshConsoleConnection{session: session, cleanup: func() {
		removeKey()
		releaseSlot()
	}}, nil
}

// authorizeSSHConsoleKey adds or removes one SSH console key in MMDS.
func authorizeSSHConsoleKey(ctx context.Context, client *api.Client, index string, authorizedKey any) error {
	patch := map[string]any{
		"latest": map[string]any{
			"meta-data": map[string]any{
				"public-keys": map[string]any{index: sshConsoleKeyValue(authorizedKey)},
			},
		},
	}
	return client.PatchMMDS(ctx, patch)
}

// sshConsoleKeyValue wraps a key for MMDS, or nil to remove the entry.
func sshConsoleKeyValue(authorizedKey any) any {
	if authorizedKey == nil {
		return nil
	}
	return map[string]any{"openssh-key": authorizedKey}
}

// sshConsoleKeyIndex names one session's MMDS key slot. A random index keeps
// concurrent sessions from replacing each other's keys.
func sshConsoleKeyIndex() (string, error) {
	buffer := make([]byte, 8)
	if _, err := rand.Read(buffer); err != nil {
		return "", fmt.Errorf("ssh key index: %w", err)
	}
	return "console-" + hex.EncodeToString(buffer), nil
}

// sshConsoleConnection runs cleanup when the SSH session closes.
type sshConsoleConnection struct {
	session *sshSession
	cleanup func()
}

// Read returns guest output.
func (c *sshConsoleConnection) Read(buffer []byte) (int, error) { return c.session.Read(buffer) }

// Write sends viewer input to the guest.
func (c *sshConsoleConnection) Write(buffer []byte) (int, error) { return c.session.Write(buffer) }

// Resize sets the guest terminal size.
func (c *sshConsoleConnection) Resize(cols, rows uint16) error { return c.session.Resize(cols, rows) }

// Close ends the session, removes the MMDS key, and frees the session slot.
func (c *sshConsoleConnection) Close() error {
	err := c.session.Close()
	c.cleanup()
	return err
}

// sshKeyPair is an ephemeral SSH key for one console session.
type sshKeyPair struct {
	privatePEM    []byte
	authorizedKey string
}

// generateSSHKey returns an ed25519 key pair in the formats SSH and MMDS need.
func generateSSHKey() (sshKeyPair, error) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return sshKeyPair{}, fmt.Errorf("generate ssh key: %w", err)
	}
	block, err := ssh.MarshalPrivateKey(private, "atlas-ssh-console")
	if err != nil {
		return sshKeyPair{}, fmt.Errorf("marshal ssh key: %w", err)
	}
	signer, err := ssh.NewPublicKey(public)
	if err != nil {
		return sshKeyPair{}, fmt.Errorf("build ssh public key: %w", err)
	}
	comment := fmt.Sprintf("atlas-ssh-console-%d", time.Now().Unix())
	authorizedKey := fmt.Sprintf("%s %s", strings.TrimSpace(string(ssh.MarshalAuthorizedKey(signer))), comment)
	return sshKeyPair{
		privatePEM:    pem.EncodeToMemory(block),
		authorizedKey: authorizedKey,
	}, nil
}

// sshSession is one interactive SSH session bridged over a PTY.
type sshSession struct {
	master  *os.File
	command *exec.Cmd
	keyPath string
}

// startSSHSession runs ssh inside the network namespace on a PTY and returns the
// session. The guest must already authorize the key. namespace is the name passed
// to `ip netns exec`. keyDir holds the temporary private key and should be on a
// runtime file system, so a crash does not leave the key on disk across a reboot.
func startSSHSession(ctx context.Context, keyDir, namespace, user, host string, privatePEM []byte) (*sshSession, error) {
	if err := os.MkdirAll(keyDir, 0o700); err != nil {
		return nil, fmt.Errorf("create ssh key directory: %w", err)
	}
	keyFile, err := os.CreateTemp(keyDir, "atlas-ssh-*.key")
	if err != nil {
		return nil, fmt.Errorf("create ssh key file: %w", err)
	}
	keyPath := keyFile.Name()
	if err := writeSSHKey(keyFile, privatePEM); err != nil {
		os.Remove(keyPath)
		return nil, err
	}

	command := exec.CommandContext(ctx, "ip", "netns", "exec", namespace,
		"ssh", "-tt",
		"-i", keyPath,
		"-o", "StrictHostKeyChecking=no",
		"-o", "UserKnownHostsFile=/dev/null",
		"-o", "LogLevel=ERROR",
		"-o", "ConnectTimeout=10",
		fmt.Sprintf("%s@%s", user, host),
	)
	// ssh forwards its own TERM to the guest PTY. metald has no usable TERM, so set
	// one the guest terminfo knows, or full-screen tools fail to open the terminal.
	command.Env = append(environmentWithoutTerm(), "TERM=xterm-256color")

	master, err := pty.Start(command)
	if err != nil {
		os.Remove(keyPath)
		return nil, fmt.Errorf("start ssh: %w", err)
	}
	return &sshSession{master: master, command: command, keyPath: keyPath}, nil
}

// environmentWithoutTerm returns the process environment with TERM removed, so
// the caller can set one value that wins.
func environmentWithoutTerm() []string {
	environment := os.Environ()
	filtered := environment[:0]
	for _, entry := range environment {
		if !strings.HasPrefix(entry, "TERM=") {
			filtered = append(filtered, entry)
		}
	}
	return filtered
}

// writeSSHKey stores the private key with owner-only permissions, which ssh
// requires before it will use a key file.
func writeSSHKey(file *os.File, privatePEM []byte) error {
	defer file.Close()
	if err := file.Chmod(0o600); err != nil {
		return fmt.Errorf("secure ssh key file: %w", err)
	}
	if _, err := file.Write(privatePEM); err != nil {
		return fmt.Errorf("write ssh key file: %w", err)
	}
	return nil
}

// Read returns output from the ssh process PTY.
func (s *sshSession) Read(buffer []byte) (int, error) { return s.master.Read(buffer) }

// Write sends input to the ssh process PTY.
func (s *sshSession) Write(buffer []byte) (int, error) { return s.master.Write(buffer) }

// Resize sets the guest terminal size.
func (s *sshSession) Resize(cols, rows uint16) error {
	return pty.Setsize(s.master, &pty.Winsize{Rows: rows, Cols: cols})
}

// Close ends the SSH process and removes the temporary key file.
func (s *sshSession) Close() error {
	err := s.master.Close()
	if s.command.Process != nil {
		_ = s.command.Process.Kill()
	}
	_ = s.command.Wait()
	os.Remove(s.keyPath)
	return err
}
