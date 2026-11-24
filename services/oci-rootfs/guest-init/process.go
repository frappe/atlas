package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const defaultPath = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

var (
	processMutex sync.Mutex
	exitChannels = map[int]chan syscall.WaitStatus{}
)

// startProcess starts one child and returns a channel for its exit status. PID 1
// reaps every process, so the Go wait functions cannot be used for its children.
func startProcess(path string, arguments []string, attributes *os.ProcAttr) (*os.Process, <-chan syscall.WaitStatus, error) {
	processMutex.Lock()
	defer processMutex.Unlock()

	process, err := os.StartProcess(path, arguments, attributes)
	if err != nil {
		return nil, nil, err
	}
	exited := make(chan syscall.WaitStatus, 1)
	exitChannels[process.Pid] = exited
	return process, exited, nil
}

// reapProcesses collects every exited child, including orphans of the entrypoint.
func reapProcesses() {
	for {
		var status syscall.WaitStatus
		pid, err := syscall.Wait4(-1, &status, 0, nil)
		if errors.Is(err, syscall.ECHILD) {
			time.Sleep(100 * time.Millisecond)
			continue
		}
		if err != nil {
			continue
		}
		processMutex.Lock()
		if exited, found := exitChannels[pid]; found {
			exited <- status
			delete(exitChannels, pid)
		}
		processMutex.Unlock()
	}
}

// startEntrypoint starts the image program with the overrides from the VM metadata.
func startEntrypoint(configuration imageConfiguration, metadata *metadataClient) (*os.Process, <-chan syscall.WaitStatus, error) {
	attributes, err := metadata.attributes()
	if err != nil {
		return nil, nil, err
	}
	command := configuration.Cmd
	if value, found := attributes["command"]; found {
		if err := json.Unmarshal([]byte(value), &command); err != nil {
			return nil, nil, fmt.Errorf("decode the command attribute: %w", err)
		}
	}

	arguments := append(append([]string{}, configuration.Entrypoint...), command...)
	if len(arguments) == 0 {
		return nil, nil, errors.New("the image has no entrypoint and no command")
	}
	account, err := lookupAccount(configuration.User)
	if err != nil {
		return nil, nil, err
	}
	workingDirectory := configuration.WorkingDir
	if workingDirectory == "" {
		workingDirectory = "/"
	}
	if err := os.MkdirAll(workingDirectory, 0o755); err != nil {
		return nil, nil, err
	}

	environment := account.environment(configuration.environment(attributes))
	path, err := findProgram(arguments[0], environment)
	if err != nil {
		return nil, nil, err
	}
	null, err := os.Open(os.DevNull)
	if err != nil {
		return nil, nil, err
	}
	defer null.Close()

	fmt.Printf("guest-init: start %v as %d:%d\n", arguments, account.userID, account.groupID)
	return startProcess(path, arguments, &os.ProcAttr{
		Dir:   workingDirectory,
		Env:   environment,
		Files: []*os.File{null, os.Stdout, os.Stderr},
		Sys:   &syscall.SysProcAttr{Setsid: true, Credential: account.credential()},
	})
}

// runSerialShell gives the TTY console a root shell. A console token already grants guest access.
func runSerialShell(metadata *metadataClient, configuration imageConfiguration) {
	shell := findShell("")
	if shell == "" {
		fmt.Println("guest-init: the image has no shell for the TTY console")
		return
	}
	for {
		tty, err := os.OpenFile("/dev/ttyS0", os.O_RDWR, 0)
		if err != nil {
			fmt.Println("guest-init: open serial console:", err)
			return
		}
		account, err := lookupAccount("root")
		if err != nil {
			account = userAccount{home: "/root"}
		}
		attributes, err := metadata.attributes()
		if err != nil {
			fmt.Println("guest-init: serial shell:", err)
		}
		_, exited, err := startProcess(shell, []string{"-" + filepath.Base(shell)}, &os.ProcAttr{
			Dir:   account.home,
			Env:   setVariables(account.environment(configuration.environment(attributes)), "TERM=linux"),
			Files: []*os.File{tty, tty, tty},
			Sys:   &syscall.SysProcAttr{Setsid: true, Setctty: true, Ctty: 0},
		})
		tty.Close()
		if err != nil {
			fmt.Println("guest-init: start serial shell:", err)
			return
		}
		<-exited
		time.Sleep(time.Second)
	}
}

// findShell returns the preferred shell, or a common one that exists in the image.
func findShell(preferred string) string {
	for _, candidate := range []string{preferred, "/bin/bash", "/bin/sh", "/busybox/sh"} {
		if information, err := os.Stat(candidate); candidate != "" && err == nil && !information.IsDir() {
			return candidate
		}
	}
	return ""
}

// findProgram resolves a command with the PATH of its environment, as a container runtime does.
func findProgram(name string, environment []string) (string, error) {
	if strings.Contains(name, "/") {
		return name, nil
	}
	searchPath := defaultPath
	for _, entry := range environment {
		if value, found := strings.CutPrefix(entry, "PATH="); found {
			searchPath = value
		}
	}
	for _, directory := range filepath.SplitList(searchPath) {
		candidate := filepath.Join(directory, name)
		if information, err := os.Stat(candidate); err == nil && !information.IsDir() && information.Mode()&0o111 != 0 {
			return candidate, nil
		}
	}
	return "", fmt.Errorf("%s is not in PATH", name)
}

// userAccount is one entry of /etc/passwd, or a numeric identity with no entry.
type userAccount struct {
	name            string
	userID, groupID uint32
	groups          []uint32
	home, shell     string
}

func (account userAccount) credential() *syscall.Credential {
	return &syscall.Credential{Uid: account.userID, Gid: account.groupID, Groups: account.groups}
}

// environment adds the account variables that a caller did not set.
func (account userAccount) environment(environment []string) []string {
	result := slices.Clone(environment)
	for _, variable := range []string{"PATH=" + defaultPath, "HOME=" + account.home} {
		name, _, _ := strings.Cut(variable, "=")
		if !slices.ContainsFunc(result, func(entry string) bool { return strings.HasPrefix(entry, name+"=") }) {
			result = append(result, variable)
		}
	}
	return result
}

// lookupAccount resolves the image USER value: user, uid, user:group, or uid:gid.
func lookupAccount(user string) (userAccount, error) {
	name, group, hasGroup := strings.Cut(user, ":")
	if name == "" {
		name = "root"
	}
	account, err := findAccount(name)
	if err != nil {
		return account, err
	}
	if hasGroup {
		account.groupID, err = findGroup(group)
		return account, err
	}
	account.groups, err = findSupplementaryGroups(account.name)
	return account, err
}

// findSupplementaryGroups returns the groups in /etc/group that list the user as a member.
func findSupplementaryGroups(user string) ([]uint32, error) {
	data, err := os.ReadFile("/etc/group")
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var groups []uint32
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Split(line, ":")
		if len(fields) < 4 || !slices.Contains(strings.Split(fields[3], ","), user) {
			continue
		}
		if identifier, err := strconv.ParseUint(fields[2], 10, 32); err == nil {
			groups = append(groups, uint32(identifier))
		}
	}
	return groups, nil
}

func findAccount(name string) (userAccount, error) {
	data, err := os.ReadFile("/etc/passwd")
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return userAccount{}, err
	}
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Split(line, ":")
		if len(fields) >= 7 && (fields[0] == name || fields[2] == name) {
			userID, userErr := strconv.ParseUint(fields[2], 10, 32)
			groupID, groupErr := strconv.ParseUint(fields[3], 10, 32)
			if userErr != nil || groupErr != nil {
				return userAccount{}, fmt.Errorf("invalid /etc/passwd entry for %s", name)
			}
			return userAccount{name: fields[0], userID: uint32(userID), groupID: uint32(groupID), home: fields[5], shell: fields[6]}, nil
		}
	}
	if name == "root" {
		return userAccount{name: "root", home: "/root"}, nil
	}
	identifier, err := strconv.ParseUint(name, 10, 32)
	if err != nil {
		return userAccount{}, fmt.Errorf("user %q is not in /etc/passwd", name)
	}
	return userAccount{name: name, userID: uint32(identifier), home: "/"}, nil
}

func findGroup(name string) (uint32, error) {
	if identifier, err := strconv.ParseUint(name, 10, 32); err == nil {
		return uint32(identifier), nil
	}
	data, err := os.ReadFile("/etc/group")
	if err != nil {
		return 0, err
	}
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Split(line, ":")
		if len(fields) >= 3 && fields[0] == name {
			identifier, err := strconv.ParseUint(fields[2], 10, 32)
			return uint32(identifier), err
		}
	}
	return 0, fmt.Errorf("group %q is not in /etc/group", name)
}
