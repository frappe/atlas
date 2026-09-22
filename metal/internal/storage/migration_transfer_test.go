package storage

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"io"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	platform "github.com/frappe/atlas/metal/internal/platform"
)

type fakeRunner struct {
	runErr   map[string]error
	outputs  map[string]string
	outErr   map[string]error
	combined map[string]string
	calls    []string
}

func commandKey(args ...string) string { return strings.Join(args, " ") }

func (f *fakeRunner) Run(_ context.Context, _ string, args ...string) error {
	f.calls = append(f.calls, commandKey(args...))
	return f.runErr[commandKey(args...)]
}

func (f *fakeRunner) Output(_ context.Context, _ string, args ...string) (string, error) {
	f.calls = append(f.calls, commandKey(args...))
	key := commandKey(args...)
	return f.outputs[key], f.outErr[key]
}

func (f *fakeRunner) CombinedOutput(_ context.Context, _ string, args ...string) (string, error) {
	f.calls = append(f.calls, commandKey(args...))
	return f.combined[commandKey(args...)], nil
}

func newTransfer(runner platform.Runner) *MigrationTransfer {
	return &MigrationTransfer{pool: &ZFSPool{name: "metal"}, runner: runner}
}

func TestSendArgumentsFormsFullIncrementalAndResume(t *testing.T) {
	transfer := newTransfer(&fakeRunner{})

	full := transfer.sendArguments("vm-1", "migration-m1-2", "", "", false)
	if commandKey(full...) != "send metal/vms/vm-1@migration-m1-2" {
		t.Fatalf("full = %v", full)
	}
	incremental := transfer.sendArguments("vm-1", "migration-m1-2", "migration-m1-1", "", false)
	if commandKey(incremental...) != "send -i metal/vms/vm-1@migration-m1-1 metal/vms/vm-1@migration-m1-2" {
		t.Fatalf("incremental = %v", incremental)
	}
	resume := transfer.sendArguments("vm-1", "migration-m1-2", "migration-m1-1", "token-xyz", false)
	if commandKey(resume...) != "send -t token-xyz" {
		t.Fatalf("resume = %v", resume)
	}
	estimate := transfer.sendArguments("vm-1", "migration-m1-2", "migration-m1-1", "", true)
	if commandKey(estimate...) != "send -nP -i metal/vms/vm-1@migration-m1-1 metal/vms/vm-1@migration-m1-2" {
		t.Fatalf("estimate = %v", estimate)
	}
}

// writeNodeTLSMaterial writes a throwaway authority and node key pair and
// returns a configuration that points at them.
func writeNodeTLSMaterial(t *testing.T) MigrationTLSConfig {
	t.Helper()

	authorityKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "test authority"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		IsCA:                  true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
	}
	authorityDER, err := x509.CreateCertificate(rand.Reader, template, template, &authorityKey.PublicKey, authorityKey)
	if err != nil {
		t.Fatal(err)
	}
	authority, err := x509.ParseCertificate(authorityDER)
	if err != nil {
		t.Fatal(err)
	}

	nodeKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	nodeTemplate := &x509.Certificate{
		SerialNumber: big.NewInt(2),
		Subject:      pkix.Name{CommonName: "node"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		IPAddresses:  []net.IP{net.ParseIP("fdab::12")},
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
	}
	nodeDER, err := x509.CreateCertificate(rand.Reader, nodeTemplate, authority, &nodeKey.PublicKey, authorityKey)
	if err != nil {
		t.Fatal(err)
	}

	directory := t.TempDir()
	write := func(name string, block *pem.Block) string {
		path := filepath.Join(directory, name)
		if err := os.WriteFile(path, pem.EncodeToMemory(block), 0o600); err != nil {
			t.Fatal(err)
		}
		return path
	}
	return MigrationTLSConfig{
		CAFile:          write("ca.crt", &pem.Block{Type: "CERTIFICATE", Bytes: authorityDER}),
		CertificateFile: write("node.crt", &pem.Block{Type: "CERTIFICATE", Bytes: nodeDER}),
		PrivateKeyFile:  write("node.key", &pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(nodeKey)}),
		ListenAddress:   "[fdab::12]:9002",
		TransferPort:    9002,
	}
}

func TestSnapshotTLSConfigurationsRequireMutualTLSAndPinTheSourceHost(t *testing.T) {
	transfer := &MigrationTransfer{tls: writeNodeTLSMaterial(t)}

	server, err := transfer.serverTLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	if server.ClientAuth != tls.RequireAndVerifyClientCert {
		t.Fatalf("client auth = %v, want RequireAndVerifyClientCert", server.ClientAuth)
	}
	if server.MinVersion != tls.VersionTLS13 || server.ClientCAs == nil || len(server.Certificates) != 1 {
		t.Fatalf("server configuration = %+v", server)
	}

	client, err := transfer.clientTLSConfig("fdab::11")
	if err != nil {
		t.Fatal(err)
	}
	if client.ServerName != "fdab::11" {
		t.Fatalf("server name = %q, want fdab::11", client.ServerName)
	}
	if client.MinVersion != tls.VersionTLS13 || client.RootCAs == nil || len(client.Certificates) != 1 {
		t.Fatalf("client configuration = %+v", client)
	}
	if client.InsecureSkipVerify {
		t.Fatal("the destination must verify the source node certificate")
	}
}

func TestSnapshotTLSConfigurationRejectsAnAuthorityFileWithoutACertificate(t *testing.T) {
	configuration := writeNodeTLSMaterial(t)
	if err := os.WriteFile(configuration.CAFile, []byte("not a certificate"), 0o600); err != nil {
		t.Fatal(err)
	}
	transfer := &MigrationTransfer{tls: configuration}

	if _, err := transfer.serverTLSConfig(); err == nil {
		t.Fatal("an authority file with no certificate must fail")
	}
}

func TestSnapshotRelayCopiesTheStreamToTheDestinationAndStopsOnCancel(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	payload := bytes.Repeat([]byte("zfs"), 100_000)
	received := make(chan []byte, 1)
	go func() {
		connection, dialError := net.Dial("tcp", listener.Addr().String())
		if dialError != nil {
			received <- nil
			return
		}
		defer connection.Close()
		body, _ := io.ReadAll(connection)
		received <- body
	}()

	if err := relaySnapshotToDestination(context.Background(), listener, bytes.NewReader(payload)); err != nil {
		t.Fatal(err)
	}
	if body := <-received; !bytes.Equal(body, payload) {
		t.Fatalf("destination received %d bytes, want %d", len(body), len(payload))
	}

	idle, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	if err := relaySnapshotToDestination(cancelled, idle, bytes.NewReader(payload)); err == nil {
		t.Fatal("a cancelled relay must not wait for a destination")
	}
}

func TestSnapshotRelayStopsWhenNoDestinationConnects(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	// The accept wait is bounded, so a destination that never dials cannot hold the
	// fixed transfer port or keep the source lock looking busy.
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	err = relaySnapshotToDestination(ctx, listener, bytes.NewReader([]byte("zfs")))

	if err == nil {
		t.Fatal("a relay with no destination must end")
	}
}

func TestMigrationHostUsesTheCoordinationURLHost(t *testing.T) {
	host, err := migrationHost("https://[fdab::11]:9001")
	if err != nil {
		t.Fatal(err)
	}
	if host != "fdab::11" {
		t.Fatalf("host = %q, want fdab::11", host)
	}
}

func TestCreateSnapshotIgnoresAlreadyExists(t *testing.T) {
	runner := &fakeRunner{runErr: map[string]error{
		"snapshot metal/vms/vm-1@migration-m1-1": errors.New("cannot create snapshot: dataset already exists"),
	}}
	if err := newTransfer(runner).CreateSnapshot(context.Background(), "vm-1", "migration-m1-1"); err != nil {
		t.Fatalf("create existing snapshot = %v, want nil", err)
	}
}

func TestRemoveSnapshotIgnoresMissing(t *testing.T) {
	for _, message := range []string{
		"could not find any snapshots to destroy; check snapshot names.",
		"cannot open 'metal/vms/vm-1': dataset does not exist",
	} {
		runner := &fakeRunner{runErr: map[string]error{
			"destroy metal/vms/vm-1@migration-m1-1": errors.New(message),
		}}
		if err := newTransfer(runner).RemoveSnapshot(context.Background(), "vm-1", "migration-m1-1"); err != nil {
			t.Fatalf("remove missing snapshot after %q = %v, want nil", message, err)
		}
	}
}

func TestAbortReceiveIgnoresNoResumableStateAndRemovesTheDataset(t *testing.T) {
	runner := &fakeRunner{runErr: map[string]error{
		"recv -A metal/vms/vm-1": errors.New("'metal/vms/vm-1' does not have any resumable receive state to abort"),
	}}
	if err := newTransfer(runner).AbortReceive(context.Background(), "vm-1"); err != nil {
		t.Fatalf("abort receive = %v, want nil", err)
	}
	if got := strings.Join(runner.calls, "|"); got != "recv -A metal/vms/vm-1|destroy -r metal/vms/vm-1" {
		t.Fatalf("calls = %v", runner.calls)
	}
}

func TestAbortReceiveIgnoresAMissingDataset(t *testing.T) {
	runner := &fakeRunner{runErr: map[string]error{
		"recv -A metal/vms/vm-1":    errors.New("cannot open 'metal/vms/vm-1': dataset does not exist"),
		"destroy -r metal/vms/vm-1": errors.New("cannot open 'metal/vms/vm-1': dataset does not exist"),
	}}
	if err := newTransfer(runner).AbortReceive(context.Background(), "vm-1"); err != nil {
		t.Fatalf("abort receive on a missing dataset = %v, want nil", err)
	}
}

func TestSnapshotGUIDReadsTheValue(t *testing.T) {
	runner := &fakeRunner{outputs: map[string]string{
		"get -Hp -o value guid metal/vms/vm-1@migration-m1-1": "12345678901234567890\n",
	}}
	guid, err := newTransfer(runner).SnapshotGUID(context.Background(), "vm-1", "migration-m1-1")
	if err != nil {
		t.Fatal(err)
	}
	if guid != "12345678901234567890" {
		t.Fatalf("guid = %q", guid)
	}
}

func TestEstimateStreamBytesParsesFullAndIncremental(t *testing.T) {
	runner := &fakeRunner{outputs: map[string]string{
		"send -nP metal/vms/vm-1@migration-m1-1":                                  "full\tmetal/vms/vm-1@migration-m1-1\t268435456\nsize\t268435456\n",
		"send -nP -i metal/vms/vm-1@migration-m1-1 metal/vms/vm-1@migration-m1-2": "incremental\tmigration-m1-1\tmetal/vms/vm-1@migration-m1-2\t1048576\nsize\t1048576\n",
	}}
	transfer := newTransfer(runner)

	full, err := transfer.EstimateStreamBytes(context.Background(), "vm-1", "migration-m1-1", "")
	if err != nil || full != 268435456 {
		t.Fatalf("full estimate = %d, %v", full, err)
	}
	incremental, err := transfer.EstimateStreamBytes(context.Background(), "vm-1", "migration-m1-2", "migration-m1-1")
	if err != nil || incremental != 1048576 {
		t.Fatalf("incremental estimate = %d, %v", incremental, err)
	}
}

func TestReceiveResumeTokenHandlesDashAndMissing(t *testing.T) {
	runner := &fakeRunner{
		outputs: map[string]string{"get -Hp -o value receive_resume_token metal/vms/vm-1": "-\n"},
	}
	token, err := newTransfer(runner).ReceiveResumeToken(context.Background(), "vm-1")
	if err != nil || token != "" {
		t.Fatalf("dash token = %q, %v", token, err)
	}

	runner = &fakeRunner{outErr: map[string]error{
		"get -Hp -o value receive_resume_token metal/vms/vm-2": errors.New("dataset does not exist"),
	}}
	token, err = newTransfer(runner).ReceiveResumeToken(context.Background(), "vm-2")
	if err != nil || token != "" {
		t.Fatalf("missing dataset token = %q, %v", token, err)
	}
}

func TestDestinationDatasetExists(t *testing.T) {
	runner := &fakeRunner{}
	present, err := newTransfer(runner).DestinationDatasetExists(context.Background(), "vm-1")
	if err != nil || !present {
		t.Fatalf("present = %v, %v", present, err)
	}

	runner = &fakeRunner{runErr: map[string]error{
		"list metal/vms/vm-2": errors.New("dataset does not exist"),
	}}
	present, err = newTransfer(runner).DestinationDatasetExists(context.Background(), "vm-2")
	if err != nil || present {
		t.Fatalf("absent = %v, %v", present, err)
	}
}

func TestVerifyResumeTokenChecksTheDestinationSnapshot(t *testing.T) {
	runner := &fakeRunner{combined: map[string]string{
		"send -nvt good": "resume token contents:\n\ttoname = metal/vms/vm-1@migration-m1-2\n",
		"send -nvt bad":  "resume token contents:\n\ttoname = metal/vms/other-vm@migration-x-1\n",
	}}
	transfer := newTransfer(runner)

	if err := transfer.verifyResumeToken(context.Background(), "vm-1", "migration-m1-2", "good"); err != nil {
		t.Fatalf("matching token = %v, want nil", err)
	}
	if err := transfer.verifyResumeToken(context.Background(), "vm-1", "migration-m1-2", "bad"); err == nil {
		t.Fatal("mismatched token = nil, want an error")
	}
}

func TestParseSendSizeBytesRejectsMissingSize(t *testing.T) {
	if _, err := parseSendSizeBytes("full\tsnap\t10\n"); err == nil {
		t.Fatal("want an error when no size line is present")
	}
}
