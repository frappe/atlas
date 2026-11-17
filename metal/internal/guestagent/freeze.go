package guestagent

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"slices"
	"strings"
	"sync"
	"time"

	"golang.org/x/sys/unix"
)

const (
	// FIFREEZE and FITHAW from linux/fs.h. golang.org/x/sys/unix does not define them.
	freezeRequest = 0xC0045877
	thawRequest   = 0xC0045878
	// maximumFreezeDuration thaws the guest when metald never closes the connection.
	maximumFreezeDuration = 30 * time.Second
)

// FreezeFilesystems freezes each writable block file system and replies "frozen".
// It thaws when metald closes the connection, ctx ends, or maximumFreezeDuration
// passes, also while a freeze is still in progress. It writes no log line while
// frozen, because a log write would block.
func FreezeFilesystems(ctx context.Context, connection io.ReadWriter) error {
	mountPoints, err := freezableMountPoints("/proc/self/mounts")
	if err != nil {
		return WriteReply(connection, err)
	}

	ctx, cancel := context.WithTimeout(ctx, maximumFreezeDuration)
	defer cancel()
	go func() {
		_, _ = io.Copy(io.Discard, connection)
		cancel()
	}()
	frozen := &frozenFilesystems{}
	defer frozen.thaw()
	go func() {
		<-ctx.Done()
		frozen.thaw()
	}()

	if err := frozen.freeze(mountPoints); err != nil {
		return WriteReply(connection, err)
	}
	if _, err := fmt.Fprintln(connection, "frozen"); err != nil {
		return err
	}
	<-ctx.Done()
	return nil
}

// frozenFilesystems lets the watchdog goroutine thaw while freeze still runs,
// because a freeze ioctl can block until outstanding writes finish.
type frozenFilesystems struct {
	mutex  sync.Mutex
	files  []*os.File
	thawed bool
}

func (frozen *frozenFilesystems) freeze(mountPoints []string) error {
	for _, mountPoint := range mountPoints {
		file, err := os.Open(mountPoint)
		if err != nil {
			return err
		}
		err = unix.IoctlSetInt(int(file.Fd()), freezeRequest, 0)
		if errors.Is(err, unix.EOPNOTSUPP) {
			file.Close()
			continue
		}
		if err != nil {
			file.Close()
			return fmt.Errorf("freeze %s: %w", mountPoint, err)
		}
		if !frozen.add(file) {
			return errors.New("freeze took too long")
		}
	}
	return nil
}

// add keeps a frozen file system, or thaws it at once when thaw already ran.
func (frozen *frozenFilesystems) add(file *os.File) bool {
	frozen.mutex.Lock()
	defer frozen.mutex.Unlock()
	if frozen.thawed {
		thawFile(file)
		return false
	}
	frozen.files = append(frozen.files, file)
	return true
}

func (frozen *frozenFilesystems) thaw() {
	frozen.mutex.Lock()
	defer frozen.mutex.Unlock()
	frozen.thawed = true
	for _, file := range slices.Backward(frozen.files) {
		thawFile(file)
	}
	frozen.files = nil
}

func thawFile(file *os.File) {
	_ = unix.IoctlSetInt(int(file.Fd()), thawRequest, 0)
	file.Close()
}

// freezableMountPoints returns one writable mount point for each block device.
// A bind mount shares its device's file system, which can be frozen only once.
func freezableMountPoints(path string) ([]string, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	var mountPoints []string
	devices := map[string]bool{}
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		// A line is "<device> <mount point> <type> <options> 0 0".
		fields := strings.Fields(scanner.Text())
		if len(fields) < 4 || !strings.HasPrefix(fields[0], "/dev/") || devices[fields[0]] {
			continue
		}
		if !slices.Contains(strings.Split(fields[3], ","), "rw") {
			continue
		}
		devices[fields[0]] = true
		mountPoints = append(mountPoints, fields[1])
	}
	return mountPoints, scanner.Err()
}
