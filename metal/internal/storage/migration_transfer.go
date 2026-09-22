package storage

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"

	platform "github.com/frappe/atlas/metal/internal/platform"
)

// MigrationTransfer runs snapshot, transfer, resume, GUID, and cleanup steps.
type MigrationTransfer struct {
	pool   *ZFSPool
	runner platform.Runner
	tls    MigrationTLSConfig
}

// copyBufferBytes is the snapshot relay buffer size.
const copyBufferBytes = 1 << 20

// stallTimeout ends a stream that makes no progress.
const stallTimeout = 2 * time.Minute

// acceptTimeout releases the fixed transfer port when a destination does not connect.
const acceptTimeout = 2 * time.Minute

// MigrationTLSConfig configures the one-shot mutual-TLS snapshot transport.
type MigrationTLSConfig struct {
	CAFile          string
	CertificateFile string
	PrivateKeyFile  string
	ListenAddress   string
	TransferPort    int
}

// NewMigrationTransfer returns a transfer helper for one ZFS pool.
func NewMigrationTransfer(pool *ZFSPool, configuration MigrationTLSConfig) *MigrationTransfer {
	return &MigrationTransfer{pool: pool, runner: platform.HostRunner{}, tls: configuration}
}

// SnapshotServer owns one source-side ZFS and TLS pipeline.
type SnapshotServer struct {
	done chan error
}

// SourceStream is one running source-side snapshot transfer.
type SourceStream interface {
	Wait() error
}

// Wait waits until the source pipeline exits.
func (server *SnapshotServer) Wait() error {
	return <-server.done
}

// StartSnapshotServer starts a one-way mutual-TLS source stream after binding its listener.
// It must stay one way: the destination never writes, so any read of the connection blocks for ever.
func (transfer *MigrationTransfer) StartSnapshotServer(ctx context.Context, virtualMachineID, snapshotName, baseSnapshotName, resumeToken string) (SourceStream, error) {
	if err := transfer.validateTLS(); err != nil {
		return nil, err
	}
	if resumeToken != "" {
		if err := transfer.verifyResumeToken(ctx, virtualMachineID, snapshotName, resumeToken); err != nil {
			return nil, err
		}
	}

	configuration, err := transfer.serverTLSConfig()
	if err != nil {
		return nil, err
	}
	listener, err := tls.Listen("tcp", transfer.tls.ListenAddress, configuration)
	if err != nil {
		return nil, fmt.Errorf("listen for migration stream on %s: %w", transfer.tls.ListenAddress, err)
	}

	streamContext, cancel := context.WithCancel(ctx)
	pipeReader, pipeWriter, err := os.Pipe()
	if err != nil {
		listener.Close()
		cancel()
		return nil, fmt.Errorf("create source transfer pipe: %w", err)
	}

	sendError := &strings.Builder{}
	sendCommand := exec.CommandContext(streamContext, "zfs", transfer.sendArguments(virtualMachineID, snapshotName, baseSnapshotName, resumeToken, false)...)
	sendCommand.Stdout = pipeWriter
	sendCommand.Stderr = sendError
	if err := sendCommand.Start(); err != nil {
		pipeReader.Close()
		pipeWriter.Close()
		listener.Close()
		cancel()
		return nil, fmt.Errorf("start zfs send: %w", err)
	}
	pipeWriter.Close()

	server := &SnapshotServer{done: make(chan error, 1)}
	go func() {
		// Cancel after the send process exits.
		defer cancel()

		relayError := relaySnapshotToDestination(streamContext, listener, pipeReader)

		// Close the pipe so an early relay failure stops zfs send.
		pipeReader.Close()

		sendWaitError := sendCommand.Wait()
		server.done <- errors.Join(
			commandError("zfs send", sendWaitError, sendError.String()),
			relayError,
		)
	}()
	return server, nil
}

// relaySnapshotToDestination accepts one destination and relays the stream to it.
func relaySnapshotToDestination(ctx context.Context, listener net.Listener, stream io.Reader) error {
	closed := make(chan struct{})
	defer close(closed)

	// Bound Accept by closing the listener when the context ends.
	acceptContext, cancelAccept := context.WithTimeout(ctx, acceptTimeout)
	defer cancelAccept()
	go func() {
		select {
		case <-acceptContext.Done():
			listener.Close()
		case <-closed:
		}
	}()

	connection, err := listener.Accept()
	listener.Close()
	if err != nil {
		if ctx.Err() != nil {
			return fmt.Errorf("accept migration stream: %w", ctx.Err())
		}
		if acceptContext.Err() != nil {
			return fmt.Errorf("no destination connected to the migration stream within %s", acceptTimeout)
		}
		return fmt.Errorf("accept migration stream: %w", err)
	}
	cancelAccept()
	defer connection.Close()

	go func() {
		select {
		case <-ctx.Done():
			connection.Close()
		case <-closed:
		}
	}()

	if _, err := io.CopyBuffer(stallingWriter{connection}, stream, make([]byte, copyBufferBytes)); err != nil {
		return fmt.Errorf("send migration stream: %w", err)
	}
	// A clean close lets zfs recv see EOF.
	if closer, ok := connection.(interface{ CloseWrite() error }); ok {
		return closer.CloseWrite()
	}
	return nil
}

// ReceiveSnapshotTLS receives a mutual-TLS ZFS stream from the source host.
func (transfer *MigrationTransfer) ReceiveSnapshotTLS(ctx context.Context, virtualMachineID, sourceAddress string) error {
	if err := transfer.validateTLS(); err != nil {
		return err
	}
	host, err := migrationHost(sourceAddress)
	if err != nil {
		return err
	}

	configuration, err := transfer.clientTLSConfig(host)
	if err != nil {
		return err
	}
	dialer := &tls.Dialer{NetDialer: &net.Dialer{}, Config: configuration}
	connection, err := dialer.DialContext(ctx, "tcp", net.JoinHostPort(host, strconv.Itoa(transfer.tls.TransferPort)))
	if err != nil {
		return fmt.Errorf("connect to migration source %s: %w", host, err)
	}
	defer connection.Close()

	pipeReader, pipeWriter, err := os.Pipe()
	if err != nil {
		return fmt.Errorf("create destination transfer pipe: %w", err)
	}
	receiveError := &strings.Builder{}
	receiveCommand := exec.CommandContext(ctx, "zfs", "recv", "-s", transfer.pool.virtualMachineDataset(virtualMachineID))
	receiveCommand.Stdin = pipeReader
	receiveCommand.Stderr = receiveError
	if err := receiveCommand.Start(); err != nil {
		pipeReader.Close()
		pipeWriter.Close()
		return fmt.Errorf("start zfs receive: %w", err)
	}
	pipeReader.Close()

	finished := make(chan struct{})
	defer close(finished)
	go func() {
		select {
		case <-ctx.Done():
			connection.Close()
		case <-finished:
		}
	}()

	_, copyError := io.CopyBuffer(pipeWriter, stallingReader{connection}, make([]byte, copyBufferBytes))
	// zfs recv ends on EOF, so the write end must close before the wait.
	pipeWriter.Close()
	receiveWaitError := receiveCommand.Wait()
	if copyError != nil {
		copyError = fmt.Errorf("receive migration stream: %w", copyError)
	}
	return errors.Join(copyError, commandError("zfs receive", receiveWaitError, receiveError.String()))
}

// stallingReader fails a read that makes no progress before stallTimeout.
type stallingReader struct {
	connection net.Conn
}

func (reader stallingReader) Read(buffer []byte) (int, error) {
	if err := reader.connection.SetReadDeadline(time.Now().Add(stallTimeout)); err != nil {
		return 0, err
	}
	return reader.connection.Read(buffer)
}

// stallingWriter fails a write that makes no progress inside stallTimeout.
type stallingWriter struct {
	connection net.Conn
}

func (writer stallingWriter) Write(buffer []byte) (int, error) {
	if err := writer.connection.SetWriteDeadline(time.Now().Add(stallTimeout)); err != nil {
		return 0, err
	}
	return writer.connection.Write(buffer)
}

func (transfer *MigrationTransfer) validateTLS() error {
	if transfer.tls.CAFile == "" || transfer.tls.CertificateFile == "" || transfer.tls.PrivateKeyFile == "" || transfer.tls.ListenAddress == "" || transfer.tls.TransferPort <= 0 {
		return fmt.Errorf("migration TLS configuration is required")
	}
	return nil
}

// serverTLSConfig requires a regional node certificate from both peers.
func (transfer *MigrationTransfer) serverTLSConfig() (*tls.Config, error) {
	certificate, authority, err := transfer.loadTLSMaterial()
	if err != nil {
		return nil, err
	}
	return &tls.Config{
		Certificates: []tls.Certificate{certificate},
		ClientCAs:    authority,
		ClientAuth:   tls.RequireAndVerifyClientCert,
		MinVersion:   tls.VersionTLS13,
	}, nil
}

// clientTLSConfig verifies the source certificate against its address.
func (transfer *MigrationTransfer) clientTLSConfig(host string) (*tls.Config, error) {
	certificate, authority, err := transfer.loadTLSMaterial()
	if err != nil {
		return nil, err
	}
	return &tls.Config{
		Certificates: []tls.Certificate{certificate},
		RootCAs:      authority,
		ServerName:   host,
		MinVersion:   tls.VersionTLS13,
	}, nil
}

func (transfer *MigrationTransfer) loadTLSMaterial() (tls.Certificate, *x509.CertPool, error) {
	certificate, err := tls.LoadX509KeyPair(transfer.tls.CertificateFile, transfer.tls.PrivateKeyFile)
	if err != nil {
		return tls.Certificate{}, nil, fmt.Errorf("load migration node certificate: %w", err)
	}
	authorityPEM, err := os.ReadFile(transfer.tls.CAFile)
	if err != nil {
		return tls.Certificate{}, nil, fmt.Errorf("read migration certificate authority: %w", err)
	}
	authority := x509.NewCertPool()
	if !authority.AppendCertsFromPEM(authorityPEM) {
		return tls.Certificate{}, nil, fmt.Errorf("migration certificate authority %s holds no certificate", transfer.tls.CAFile)
	}
	return certificate, authority, nil
}

func migrationHost(address string) (string, error) {
	parsed, err := url.Parse(address)
	if err != nil || parsed.Hostname() == "" {
		return "", fmt.Errorf("invalid migration source address %q", address)
	}
	return parsed.Hostname(), nil
}

func commandError(name string, err error, output string) error {
	if err == nil {
		return nil
	}
	return fmt.Errorf("%s: %w: %s", name, err, strings.TrimSpace(output))
}

// CreateSnapshot creates one migration snapshot. A repeat is safe.
func (transfer *MigrationTransfer) CreateSnapshot(ctx context.Context, virtualMachineID, snapshotName string) error {
	// zfs snapshot: atomic read-only point-in-time copy of the VM volume.
	err := transfer.runner.Run(ctx, "zfs", "snapshot", transfer.pool.snapshot(virtualMachineID, snapshotName))
	if err != nil && strings.Contains(err.Error(), "already exists") {
		return nil
	}
	return err
}

// RemoveSnapshot destroys one migration snapshot. Missing snapshots are safe.
func (transfer *MigrationTransfer) RemoveSnapshot(ctx context.Context, virtualMachineID, snapshotName string) error {
	// zfs destroy: remove one snapshot, leaving the volume and other snapshots.
	err := transfer.runner.Run(ctx, "zfs", "destroy", transfer.pool.snapshot(virtualMachineID, snapshotName))
	if err != nil && (strings.Contains(err.Error(), "does not exist") ||
		strings.Contains(err.Error(), "could not find any snapshots to destroy")) {
		return nil
	}
	return err
}

// SnapshotGUID returns a snapshot GUID for post-receive validation.
func (transfer *MigrationTransfer) SnapshotGUID(ctx context.Context, virtualMachineID, snapshotName string) (string, error) {
	// zfs get -Hp -o value guid returns the exact GUID without a header.
	output, err := transfer.runner.Output(ctx, "zfs", "get", "-Hp", "-o", "value", "guid", transfer.pool.snapshot(virtualMachineID, snapshotName))
	if err != nil {
		return "", notFoundAware(err)
	}
	guid := strings.TrimSpace(output)
	if guid == "" || guid == "-" {
		return "", fmt.Errorf("snapshot %s has no GUID", snapshotName)
	}
	return guid, nil
}

// EstimateStreamBytes returns the full or incremental send size in bytes.
func (transfer *MigrationTransfer) EstimateStreamBytes(ctx context.Context, virtualMachineID, snapshotName, baseSnapshotName string) (int64, error) {
	// zfs send -nP reports the parseable dry-run size.
	output, err := transfer.runner.Output(ctx, "zfs", transfer.sendArguments(virtualMachineID, snapshotName, baseSnapshotName, "", true)...)
	if err != nil {
		return 0, notFoundAware(err)
	}
	return parseSendSizeBytes(output)
}

// DestinationDatasetExists reports whether the destination dataset exists.
func (transfer *MigrationTransfer) DestinationDatasetExists(ctx context.Context, virtualMachineID string) (bool, error) {
	// zfs list succeeds only when the dataset exists.
	err := transfer.runner.Run(ctx, "zfs", "list", transfer.pool.virtualMachineDataset(virtualMachineID))
	if err == nil {
		return true, nil
	}
	if strings.Contains(err.Error(), "does not exist") {
		return false, nil
	}
	return false, fmt.Errorf("check destination dataset: %w", err)
}

// ReceiveResumeToken returns an interrupted receive token, or an empty string.
func (transfer *MigrationTransfer) ReceiveResumeToken(ctx context.Context, virtualMachineID string) (string, error) {
	// zfs get receive_resume_token returns the resume token; "-" means none.
	output, err := transfer.runner.Output(ctx, "zfs", "get", "-Hp", "-o", "value", "receive_resume_token", transfer.pool.virtualMachineDataset(virtualMachineID))
	if err != nil {
		if strings.Contains(err.Error(), "does not exist") {
			return "", nil
		}
		return "", err
	}
	token := strings.TrimSpace(output)
	if token == "-" {
		return "", nil
	}
	return token, nil
}

// AbortReceive cancels an interrupted receive and removes its dataset.
func (transfer *MigrationTransfer) AbortReceive(ctx context.Context, virtualMachineID string) error {
	dataset := transfer.pool.virtualMachineDataset(virtualMachineID)
	// zfs recv -A deletes saved partial receive state.
	if err := transfer.runner.Run(ctx, "zfs", "recv", "-A", dataset); err != nil &&
		!strings.Contains(err.Error(), "does not exist") &&
		!strings.Contains(err.Error(), "does not have any resumable") {
		return fmt.Errorf("abort partial receive: %w", err)
	}
	// zfs destroy -r removes the destination dataset and migration snapshots.
	if err := transfer.runner.Run(ctx, "zfs", "destroy", "-r", dataset); err != nil &&
		!strings.Contains(err.Error(), "does not exist") {
		return fmt.Errorf("remove destination dataset: %w", err)
	}
	return nil
}

// verifyResumeToken rejects tokens for another migration snapshot.
func (transfer *MigrationTransfer) verifyResumeToken(ctx context.Context, virtualMachineID, snapshotName, resumeToken string) error {
	// zfs send -nvt reports the snapshot the resume token names.
	output, err := transfer.runner.CombinedOutput(ctx, "zfs", "send", "-nvt", resumeToken)
	if err != nil {
		return fmt.Errorf("validate resume token: %w", err)
	}
	if !strings.Contains(output, transfer.pool.snapshot(virtualMachineID, snapshotName)) {
		return fmt.Errorf("resume token does not match snapshot %s", snapshotName)
	}
	return nil
}

// sendArguments builds full, incremental, resume, and estimate arguments.
func (transfer *MigrationTransfer) sendArguments(virtualMachineID, snapshotName, baseSnapshotName, resumeToken string, estimate bool) []string {
	arguments := []string{"send"}
	if estimate {
		arguments = append(arguments, "-nP")
	}
	if resumeToken != "" {
		return append(arguments, "-t", resumeToken)
	}
	if baseSnapshotName != "" {
		arguments = append(arguments, "-i", transfer.pool.snapshot(virtualMachineID, baseSnapshotName))
	}
	return append(arguments, transfer.pool.snapshot(virtualMachineID, snapshotName))
}

// parseSendSizeBytes reads the size line of `zfs send -nP` output.
func parseSendSizeBytes(output string) (int64, error) {
	for _, line := range strings.Split(strings.TrimSpace(output), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 2 && fields[0] == "size" {
			return strconv.ParseInt(fields[1], 10, 64)
		}
	}
	return 0, fmt.Errorf("zfs send estimate has no size")
}
