package firecracker

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/frappe/atlas/metal/internal/console"
	"github.com/frappe/atlas/metal/internal/firecracker/api"
	"github.com/frappe/atlas/metal/internal/platform"
	"github.com/frappe/atlas/metal/internal/vm"
	"golang.org/x/crypto/ssh"
)

const (
	// sshConsoleUser is the guest account used by the SSH console.
	sshConsoleUser = "root"

	// sshSessionReconnectGrace is how long a session that a new metald adopts
	// waits for its viewer. The Atlas bridge reconnects within 60 seconds.
	sshSessionReconnectGrace = 2 * time.Minute

	// sshSessionCleanupTimeout bounds the cleanup of one ended session.
	sshSessionCleanupTimeout = 30 * time.Second
)

// sshSessionUnits runs the ssh client of each session as a systemd unit that
// outlives metald.
type sshSessionUnits interface {
	Start(ctx context.Context, sessionID string, command []string, ttyPath string) error
	Stop(ctx context.Context, sessionID string) error
	Wait(ctx context.Context, sessionID string) error
	List(ctx context.Context) ([]string, error)
}

// sshSession is one SSH console. The ssh client runs in its own systemd unit on
// a console PTY whose master systemd keeps, so a viewer can resume the same
// shell after a metald restart. The session ID is also its console ID and
// starts with "ssh-", so it cannot match a VM ID.
type sshSession struct {
	runtime *Runtime
	record  sshSessionRecord
	id      string
	viewers atomic.Int32
	done    chan struct{}
	endOnce sync.Once
}

// sshSessionRecord lets the next metald clean up a session that ended while no
// metald ran.
type sshSessionRecord struct {
	VirtualMachineID string `json:"virtual_machine_id"`
	KeyIndex         string `json:"key_index"`
}

// CheckMigratedGuest requires a guest SSH handshake when enabled for memory migrations.
func (runtime *Runtime) CheckMigratedGuest(ctx context.Context, input vm.RuntimeMachine) error {
	if !runtime.configuration.CheckMigrationGuestSSH {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()

	output, err := platform.RunInNetworkNamespace(ctx,
		filepath.Base(input.NetworkInterface.NetworkNamespacePath),
		"ssh-keyscan", "-T", "5", "-t", "ed25519,rsa", input.NetworkInterface.GuestIPAddress,
	)
	if err != nil {
		return fmt.Errorf("guest SSH handshake: %w", err)
	}
	if strings.TrimSpace(output) == "" {
		return fmt.Errorf("guest SSH handshake returned no host key")
	}
	return nil
}

// ConnectSSH opens an interactive SSH session to a VM guest. Authorization is a
// throwaway key pushed through MMDS and removed when the session ends, so no
// key outlives the session and the guest image carries none.
func (runtime *Runtime) ConnectSSH(ctx context.Context, input vm.RuntimeMachine) (vm.SSHSession, error) {
	random := make([]byte, 8)
	if _, err := rand.Read(random); err != nil {
		return nil, fmt.Errorf("ssh session id: %w", err)
	}
	identifier := hex.EncodeToString(random)
	session := runtime.newSSHSession("ssh-"+identifier, sshSessionRecord{
		VirtualMachineID: input.ID,
		KeyIndex:         "console-" + identifier,
	})
	if err := runtime.addSSHSession(session); err != nil {
		return nil, err
	}

	if err := session.start(ctx, input); err != nil {
		session.end()
		return nil, err
	}
	go session.endWhenClientExits()

	return session, nil
}

// ResumeSSH returns the open SSH session sessionID of a VM.
func (runtime *Runtime) ResumeSSH(virtualMachineID, sessionID string) (vm.SSHSession, error) {
	runtime.sshMutex.Lock()
	defer runtime.sshMutex.Unlock()

	session := runtime.sshSessions[sessionID]
	if session == nil || session.record.VirtualMachineID != virtualMachineID {
		return nil, fmt.Errorf("ssh session %s: %w", sessionID, vm.ErrNotFound)
	}

	return session, nil
}

// RecoverSSHSessions adopts the SSH sessions that outlived the previous metald
// and ends the ones that have no client or console any more. Call it after the
// serial broker adopts its consoles. An adopted session ends when no viewer
// returns within the reconnect grace.
func (runtime *Runtime) RecoverSSHSessions() {
	entries, err := os.ReadDir(runtime.configuration.sshSessionsDir())
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		runtime.logger.Error("read ssh sessions", "error", err)
	}

	recorded := make(map[string]bool, len(entries))
	for _, entry := range entries {
		recorded[entry.Name()] = true
		var record sshSessionRecord
		contents, err := os.ReadFile(filepath.Join(runtime.configuration.sshSessionDir(entry.Name()), "record.json"))
		if err == nil {
			err = json.Unmarshal(contents, &record)
		}
		session := runtime.newSSHSession(entry.Name(), record)
		runtime.sshMutex.Lock()
		runtime.sshSessions[session.id] = session
		runtime.sshMutex.Unlock()

		if err != nil || !runtime.serialBroker.IsOpen(session.id) {
			go session.end()
			continue
		}
		go session.endWhenClientExits()
		time.AfterFunc(sshSessionReconnectGrace, func() {
			if session.viewers.Load() == 0 {
				session.end()
			}
		})
	}
	runtime.stopSSHSessionsWithoutRecords(recorded)
	runtime.logger.Info("recovered ssh sessions", "session_count", len(entries))
}

// stopSSHSessionsWithoutRecords ends each SSH session unit that has no record.
// Nobody can resume such a shell or remove its key later.
func (runtime *Runtime) stopSSHSessionsWithoutRecords(recorded map[string]bool) {
	ctx, cancel := context.WithTimeout(context.Background(), sshSessionCleanupTimeout)
	defer cancel()

	sessionIDs, err := runtime.sshUnits.List(ctx)
	if err != nil {
		runtime.logger.Error("list ssh session units", "error", err)
		return
	}
	for _, sessionID := range sessionIDs {
		if !recorded[sessionID] {
			go runtime.newSSHSession(sessionID, sshSessionRecord{}).end()
		}
	}
}

func (runtime *Runtime) newSSHSession(id string, record sshSessionRecord) *sshSession {
	return &sshSession{runtime: runtime, record: record, id: id, done: make(chan struct{})}
}

// addSSHSession registers a session within the host-wide session limit.
func (runtime *Runtime) addSSHSession(session *sshSession) error {
	runtime.sshMutex.Lock()
	defer runtime.sshMutex.Unlock()

	if len(runtime.sshSessions) >= maxConcurrentSSHSessions {
		return fmt.Errorf("ssh session limit reached")
	}
	runtime.sshSessions[session.id] = session

	return nil
}

// ID returns the session ID that a viewer passes to resume the session.
func (session *sshSession) ID() string { return session.id }

// Attach streams the session to one viewer until the viewer leaves, ctx ends,
// or the ssh client exits.
func (session *sshSession) Attach(ctx context.Context, client io.ReadWriter, resize <-chan console.Winsize) error {
	session.viewers.Add(1)
	defer session.viewers.Add(-1)

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() {
		select {
		case <-session.done:
			cancel()
		case <-ctx.Done():
		}
	}()

	return session.runtime.serialBroker.Attach(ctx, session.id, client, resize)
}

// Close ends the session.
func (session *sshSession) Close() error {
	session.end()
	return nil
}

// start authorizes a key in the guest and starts the ssh client unit on a new console.
func (session *sshSession) start(ctx context.Context, input vm.RuntimeMachine) error {
	runtime := session.runtime
	keyPair, err := generateSSHKey()
	if err != nil {
		return err
	}

	// Write the record before the key reaches the guest, so a crash leaves a
	// record that the next metald uses to remove the key.
	directory := runtime.configuration.sshSessionDir(session.id)
	keyPath := filepath.Join(directory, "key")
	if err := writeSSHSessionFiles(directory, keyPath, keyPair.privatePEM, session.record); err != nil {
		return err
	}
	client := api.New(runtime.configuration.socketPath(input.ID))
	if err := authorizeSSHConsoleKey(ctx, client, session.record.KeyIndex, strings.TrimSpace(keyPair.authorizedKey)); err != nil {
		return fmt.Errorf("authorize ssh key: %w", err)
	}

	ipCommand, err := exec.LookPath("ip")
	if err != nil {
		return fmt.Errorf("find ip: %w", err)
	}
	command := []string{
		ipCommand, "netns", "exec", filepath.Base(input.NetworkInterface.NetworkNamespacePath),
		"ssh", "-tt",
		"-i", keyPath,
		"-o", "StrictHostKeyChecking=no",
		"-o", "UserKnownHostsFile=/dev/null",
		"-o", "LogLevel=ERROR",
		"-o", "ConnectTimeout=10",
		fmt.Sprintf("%s@%s", sshConsoleUser, input.NetworkInterface.GuestIPAddress),
	}

	if err := runtime.serialBroker.Open(session.id); err != nil {
		return err
	}
	if err := runtime.sshUnits.Start(ctx, session.id, command, runtime.configuration.consolePath(session.id)); err != nil {
		return fmt.Errorf("start ssh client: %w", err)
	}

	// systemd drops a stored master while no process holds the slave, so store
	// it only after the unit opened its terminal.
	return runtime.serialBroker.Persist(session.id)
}

// endWhenClientExits ends the session after its ssh client exits. A failed
// wait, for example when metald shuts down, leaves the session for the next metald.
func (session *sshSession) endWhenClientExits() {
	if err := session.runtime.sshUnits.Wait(context.Background(), session.id); err == nil {
		session.end()
	}
}

// end stops the ssh client and removes the console, the guest key, and the
// session files. Each step tolerates a part that is already gone.
func (session *sshSession) end() {
	session.endOnce.Do(func() {
		runtime := session.runtime
		ctx, cancel := context.WithTimeout(context.Background(), sshSessionCleanupTimeout)
		defer cancel()

		if err := runtime.sshUnits.Stop(ctx, session.id); err != nil {
			runtime.logger.Warn("stop ssh session", "session_id", session.id, "error", err)
		}
		if err := runtime.serialBroker.Close(session.id); err != nil {
			runtime.logger.Warn("close ssh session console", "session_id", session.id, "error", err)
		}
		if session.record.VirtualMachineID != "" {
			client := api.New(runtime.configuration.socketPath(session.record.VirtualMachineID))
			if err := authorizeSSHConsoleKey(ctx, client, session.record.KeyIndex, nil); err != nil {
				runtime.logger.Warn("remove ssh session key", "session_id", session.id, "error", err)
			}
		}
		if err := os.RemoveAll(runtime.configuration.sshSessionDir(session.id)); err != nil {
			runtime.logger.Warn("remove ssh session files", "session_id", session.id, "error", err)
		}

		runtime.sshMutex.Lock()
		delete(runtime.sshSessions, session.id)
		runtime.sshMutex.Unlock()
		close(session.done)
	})
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

// writeSSHSessionFiles stores the private key, with the owner-only permissions
// that ssh requires, and the session record. directory should be on a runtime
// file system, so a crash does not leave the key on disk across a reboot.
func writeSSHSessionFiles(directory, keyPath string, privatePEM []byte, record sshSessionRecord) error {
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return fmt.Errorf("create ssh session directory: %w", err)
	}
	if err := os.WriteFile(keyPath, privatePEM, 0o600); err != nil {
		return fmt.Errorf("write ssh key file: %w", err)
	}
	contents, err := json.Marshal(record)
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(directory, "record.json"), contents, 0o600); err != nil {
		return fmt.Errorf("write ssh session record: %w", err)
	}
	return nil
}
