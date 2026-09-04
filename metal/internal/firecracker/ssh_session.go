package firecracker

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/pem"
	"fmt"
	"os"
	"os/exec"
	"strings"

	"github.com/creack/pty"
	"golang.org/x/crypto/ssh"
)

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
	return sshKeyPair{
		privatePEM:    pem.EncodeToMemory(block),
		authorizedKey: string(ssh.MarshalAuthorizedKey(signer)),
	}, nil
}

// sshSession is one interactive SSH session bridged over a PTY.
type sshSession struct {
	master  *os.File
	command *exec.Cmd
	keyPath string
}

// startSSHSession runs ssh inside the network namespace on a PTY and returns the
// session. The guest must already authorize the key. namespace is the "ip netns"
// name. keyDir holds the temporary private key and should be on a runtime file
// system, so a crash does not leave the key on disk across a reboot.
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

func (s *sshSession) Read(buffer []byte) (int, error)  { return s.master.Read(buffer) }
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
