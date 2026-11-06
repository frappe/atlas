package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/binary"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/creack/pty"
	"github.com/pkg/sftp"
	"golang.org/x/crypto/ssh"
)

const (
	sshListenAddress    = ":22"
	sshHandshakeTimeout = 30 * time.Second

	terminalDrainTimeout = time.Second

	// sftpServerArgument starts this binary as the SFTP server of one session.
	sftpServerArgument = "sftp-server"
)

// hostKeyMutex stops two first connections from each writing a different key.
var hostKeyMutex sync.Mutex

// serveSSH is the guest SSH server, because the image may have no sshd. Its keys
// come from MMDS at each login, so a key change needs no restart.
func serveSSH(metadata *metadataClient, image imageConfiguration) {
	listener, err := net.Listen("tcp", sshListenAddress)
	if err != nil {
		fmt.Println("guest-init: ssh listen:", err)
		return
	}
	for {
		connection, err := listener.Accept()
		if err != nil {
			continue
		}
		go handleSSHConnection(connection, metadata, image)
	}
}

func handleSSHConnection(connection net.Conn, metadata *metadataClient, image imageConfiguration) {
	defer connection.Close()
	_ = connection.SetDeadline(time.Now().Add(sshHandshakeTimeout))

	hostKey, err := loadHostKey(metadata)
	if err != nil {
		fmt.Println("guest-init: ssh host key:", err)
		return
	}
	configuration := &ssh.ServerConfig{
		PublicKeyCallback: func(_ ssh.ConnMetadata, offered ssh.PublicKey) (*ssh.Permissions, error) {
			for _, line := range metadata.publicKeys() {
				key, _, _, _, err := ssh.ParseAuthorizedKey([]byte(line))
				if err == nil && bytes.Equal(key.Marshal(), offered.Marshal()) {
					return &ssh.Permissions{}, nil
				}
			}
			return nil, fmt.Errorf("unknown public key")
		},
	}
	configuration.AddHostKey(hostKey)

	server, channels, requests, err := ssh.NewServerConn(connection, configuration)
	if err != nil {
		return
	}
	defer server.Close()
	_ = connection.SetDeadline(time.Time{})
	go ssh.DiscardRequests(requests)

	account, err := lookupAccount(server.User())
	if err != nil {
		fmt.Println("guest-init: ssh user:", err)
		return
	}
	attributes, err := metadata.attributes()
	if err != nil {
		fmt.Println("guest-init: ssh environment:", err)
	}
	environment := account.environment(image.environment(attributes))
	for request := range channels {
		if request.ChannelType() != "session" {
			_ = request.Reject(ssh.UnknownChannelType, "only sessions are supported")
			continue
		}
		channel, sessionRequests, err := request.Accept()
		if err != nil {
			continue
		}
		go (&sshSession{channel: channel, account: account, environment: environment}).serve(sessionRequests)
	}
}

// loadHostKey returns the host key of this VM. The key name holds the VM ID, so
// a VM started from a warm image or a machine image does not share its source key.
func loadHostKey(metadata *metadataClient) (ssh.Signer, error) {
	hostKeyMutex.Lock()
	defer hostKeyMutex.Unlock()

	instance, err := metadata.read("meta-data/instance-id")
	if err != nil {
		return nil, err
	}
	instance = strings.TrimSpace(instance)
	path := filepath.Join(filepath.Dir(imageConfigurationPath), "ssh_host_ed25519_key-"+filepath.Base(instance))
	if data, err := os.ReadFile(path); err == nil {
		if block, _ := pem.Decode(data); block != nil {
			return ssh.NewSignerFromKey(ed25519.PrivateKey(block.Bytes))
		}
	}
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	data := pem.EncodeToMemory(&pem.Block{Type: "ATLAS ED25519 PRIVATE KEY", Bytes: privateKey})
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return nil, err
	}
	return ssh.NewSignerFromKey(privateKey)
}

// sshSession is one session channel: a shell or a command, with or without a
// terminal. It owns every descriptor in files, and serve closes them at the end.
type sshSession struct {
	channel      ssh.Channel
	account      userAccount
	environment  []string
	mutex        sync.Mutex
	master       *os.File
	terminal     *os.File
	term         string
	files        []*os.File
	processGroup int
	started      bool
}

func (session *sshSession) serve(requests <-chan *ssh.Request) {
	defer session.close()

	for request := range requests {
		accepted := false
		switch request.Type {
		case "pty-req":
			accepted = session.openTerminal(request.Payload)
		case "window-change":
			session.resize(request.Payload)
			accepted = true
		case "env":
			accepted = true
		case "shell":
			accepted = session.start("")
		case "exec":
			var command struct{ Value string }
			if ssh.Unmarshal(request.Payload, &command) == nil {
				accepted = session.start(command.Value)
			}
		case "subsystem":
			var subsystem struct{ Name string }
			if ssh.Unmarshal(request.Payload, &subsystem) == nil && subsystem.Name == "sftp" {
				accepted = session.startProgram("/proc/self/exe", []string{"guest-init", sftpServerArgument})
			}
		}
		if request.WantReply {
			_ = request.Reply(accepted, nil)
		}
	}
}

// close hangs up the processes of the session, as the end of a terminal does, and
// frees the descriptors. A process that ignores SIGHUP, such as one under nohup, stays.
func (session *sshSession) close() {
	session.mutex.Lock()
	defer session.mutex.Unlock()

	if session.processGroup != 0 {
		_ = syscall.Kill(-session.processGroup, syscall.SIGHUP)
	}
	for _, file := range session.files {
		file.Close()
	}
}

func (session *sshSession) openTerminal(payload []byte) bool {
	var request struct {
		Term          string
		Columns, Rows uint32
		Width, Height uint32
		Modes         string
	}
	if ssh.Unmarshal(payload, &request) != nil {
		return false
	}

	session.mutex.Lock()
	defer session.mutex.Unlock()
	if session.master != nil || session.started {
		return false
	}
	master, terminal, err := pty.Open()
	if err != nil {
		fmt.Println("guest-init: open terminal:", err)
		return false
	}
	_ = pty.Setsize(master, &pty.Winsize{Rows: uint16(request.Rows), Cols: uint16(request.Columns)})
	session.term, session.master, session.terminal = request.Term, master, terminal
	session.files = append(session.files, master, terminal)
	return true
}

func (session *sshSession) resize(payload []byte) {
	if len(payload) < 8 {
		return
	}
	session.mutex.Lock()
	defer session.mutex.Unlock()
	if session.master != nil {
		_ = pty.Setsize(session.master, &pty.Winsize{Cols: uint16(binary.BigEndian.Uint32(payload)), Rows: uint16(binary.BigEndian.Uint32(payload[4:]))})
	}
}

// start runs the login shell, or a command with the login shell.
func (session *sshSession) start(command string) bool {
	shell := findShell(session.account.shell)
	if shell == "" {
		_, _ = fmt.Fprintln(session.channel.Stderr(), "guest-init: the image has no shell")
		return false
	}
	if command == "" {
		return session.startProgram(shell, []string{"-" + filepath.Base(shell)})
	}
	return session.startProgram(shell, []string{filepath.Base(shell), "-c", command})
}

// startProgram runs one program for the session and sends its exit status.
func (session *sshSession) startProgram(path string, arguments []string) bool {
	session.mutex.Lock()
	defer session.mutex.Unlock()
	if session.started {
		return false
	}

	var output sync.WaitGroup
	streams, err := session.connectStreams(&output)
	if err != nil {
		fmt.Println("guest-init: ssh streams:", err)
		return false
	}
	account := session.account
	environment := setVariables(session.environment, "USER="+account.name, "LOGNAME="+account.name, "SHELL="+findShell(account.shell))
	if session.term != "" {
		environment = setVariables(environment, "TERM="+session.term)
	}
	attributes := &os.ProcAttr{
		Dir:   account.home,
		Env:   environment,
		Files: streams,
		Sys:   &syscall.SysProcAttr{Setsid: true, Setctty: session.terminal != nil, Credential: account.credential()},
	}
	if _, err := os.Stat(attributes.Dir); err != nil {
		attributes.Dir = "/"
	}

	process, exited, err := startProcess(path, arguments, attributes)
	for _, file := range streams {
		file.Close()
	}
	if err != nil {
		_, _ = fmt.Fprintln(session.channel.Stderr(), "guest-init:", err)
		return false
	}
	session.started, session.processGroup = true, process.Pid
	go session.sendExitStatus(exited, &output)
	return true
}

// connectStreams copies between the channel and the standard streams of the program,
// and returns the ends for the program. The output group ends with the output.
func (session *sshSession) connectStreams(output *sync.WaitGroup) ([]*os.File, error) {
	copyOutput := func(destination io.Writer, source io.Reader) {
		output.Go(func() { _, _ = io.Copy(destination, source) })
	}
	if session.terminal != nil {
		copyOutput(session.channel, session.master)
		go func() { _, _ = io.Copy(session.master, session.channel) }()
		return []*os.File{session.terminal, session.terminal, session.terminal}, nil
	}

	var pipes [3][2]*os.File
	for index := range pipes {
		read, write, err := os.Pipe()
		if err != nil {
			return nil, err
		}
		pipes[index] = [2]*os.File{read, write}
		session.files = append(session.files, read, write)
	}
	stdin, stdout, stderr := pipes[0], pipes[1], pipes[2]
	go func() {
		_, _ = io.Copy(stdin[1], session.channel)
		stdin[1].Close()
	}()
	copyOutput(session.channel, stdout[0])
	copyOutput(session.channel.Stderr(), stderr[0])
	return []*os.File{stdin[0], stdout[1], stderr[1]}, nil
}

// sendExitStatus ends the channel after the program and its output end. A background
// process can hold a terminal open, so the wait for terminal output has a limit.
func (session *sshSession) sendExitStatus(exited <-chan syscall.WaitStatus, output *sync.WaitGroup) {
	status := <-exited
	drained := make(chan struct{})
	go func() {
		output.Wait()
		close(drained)
	}()
	var limit <-chan time.Time
	if session.terminal != nil {
		limit = time.After(terminalDrainTimeout)
	}
	select {
	case <-drained:
	case <-limit:
	}

	code := status.ExitStatus()
	if status.Signaled() {
		code = 128 + int(status.Signal())
	}
	_, _ = session.channel.SendRequest("exit-status", false, ssh.Marshal(struct{ Status uint32 }{uint32(code)}))
	_ = session.channel.Close()
}

// serveSFTP runs in a child with the session user, so file access follows that user.
func serveSFTP() error {
	server, err := sftp.NewServer(struct {
		io.Reader
		io.WriteCloser
	}{os.Stdin, os.Stdout})
	if err != nil {
		return err
	}
	if err := server.Serve(); err != nil && !errors.Is(err, io.EOF) {
		return err
	}
	return nil
}
