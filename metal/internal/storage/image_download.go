package storage

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"sync/atomic"
	"time"

	"github.com/klauspost/compress/zstd"
)

// errRetryableDownload marks a failure that another attempt may recover from.
// Digest and status failures are not marked, so a wrong artifact fails at once.
var errRetryableDownload = errors.New("retryable download failure")

const (
	// downloadAttempts is how many times one artifact download is tried.
	downloadAttempts = 5

	// downloadBaseDelay is the first retry delay. Each later delay doubles it.
	downloadBaseDelay = 2 * time.Second

	// downloadTimeout bounds one whole artifact download, which can be several GiB.
	downloadTimeout = 30 * time.Minute

	// downloadBufferSize sets the write size of a download. A small one costs
	// one syscall per few bytes.
	downloadBufferSize = 1 << 20
)

// downloadDestination receives the decoded bytes of one download attempt.
type downloadDestination interface {
	open(ctx context.Context, sizeBytes int64) (io.Writer, error)
	commit() error
	discard()
}

// download fetches one artifact into destination. Retries use exponential backoff,
// and the URL is redacted in logs because it carries a signature.
func download(ctx context.Context, client *http.Client, source, expectedDigest string, destination downloadDestination, loggers ...*slog.Logger) error {
	logger := slog.Default()
	if len(loggers) > 0 && loggers[0] != nil {
		logger = loggers[0]
	}
	if _, err := parseImageURL(source); err != nil {
		return err
	}

	redactedSource := redactURL(source)

	var lastError error
	for attempt := 1; attempt <= downloadAttempts; attempt++ {
		if attempt > 1 {
			delay := downloadBaseDelay << (attempt - 2)
			logger.Warn("image download failed, retrying", "source", redactedSource, "attempt", attempt-1, "maximum_attempts", downloadAttempts, "retry_after", delay, "error", lastError)
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(delay):
			}
		}
		err := downloadOnce(ctx, client, source, expectedDigest, destination)
		if err == nil {
			return nil
		}
		destination.discard()
		if !errors.Is(err, errRetryableDownload) {
			return fmt.Errorf("download %s: %w", redactedSource, err)
		}
		lastError = err
	}
	return fmt.Errorf("download %s after %d attempts: %w", redactedSource, downloadAttempts, lastError)
}

// downloadOnce makes one attempt and verifies the digest as it writes.
func downloadOnce(ctx context.Context, client *http.Client, source, expectedDigest string, destination downloadDestination) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, source, nil)
	if err != nil {
		return fmt.Errorf("create request")
	}
	result, err := client.Do(request)
	if err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return fmt.Errorf("%w: request failed", errRetryableDownload)
	}
	defer result.Body.Close()
	if result.StatusCode != http.StatusOK {
		statusError := fmt.Errorf("HTTP status %d", result.StatusCode)
		if result.StatusCode == http.StatusRequestTimeout || result.StatusCode == http.StatusTooManyRequests || result.StatusCode >= 500 {
			return fmt.Errorf("%w: %v", errRetryableDownload, statusError)
		}
		return statusError
	}

	// Count wire bytes. Compression makes them differ from bytes on disk.
	transferred := &countingReader{reader: result.Body, counter: new(atomic.Int64)}
	body, sizeBytes, closeBody, err := decompressedBody(transferred, result.ContentLength)
	if err != nil {
		return err
	}
	defer closeBody()

	writer, err := destination.open(ctx, sizeBytes)
	if err != nil {
		return err
	}
	reader := newSHA256Reader(body)
	err = copyInFullBuffers(writer, reader)
	digest := reader.Sum()
	if err != nil {
		return err
	}
	if result.ContentLength >= 0 && transferred.counter.Load() != result.ContentLength {
		return fmt.Errorf("truncated response")
	}
	if reader.readBytes != sizeBytes {
		return fmt.Errorf("%w: decoded %d of %d artifact bytes", ErrImageIntegrity, reader.readBytes, sizeBytes)
	}
	if hex.EncodeToString(digest) != expectedDigest {
		return fmt.Errorf("%w: SHA-256 digest mismatch", ErrImageIntegrity)
	}
	return destination.commit()
}

// copyInFullBuffers writes whole downloadBufferSize chunks, so a ZFS volume gets
// aligned writes.
func copyInFullBuffers(writer io.Writer, reader io.Reader) error {
	buffer := make([]byte, downloadBufferSize)
	for {
		read, err := io.ReadFull(reader, buffer)
		if read > 0 {
			if _, writeError := writer.Write(buffer[:read]); writeError != nil {
				return writeError
			}
		}
		if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
			return nil
		}
		if err != nil {
			return err
		}
	}
}

// zstdMagic starts every zstd frame. A raw disk image never begins with it.
var zstdMagic = []byte{0x28, 0xB5, 0x2F, 0xFD}

// decompressedBody detects zstd content and returns decoded artifact bytes and
// their size. The size is the zstd frame content size, or the HTTP length of a
// raw artifact.
func decompressedBody(body io.Reader, contentLength int64) (io.Reader, int64, func(), error) {
	buffered := bufio.NewReaderSize(body, downloadBufferSize)
	prefix, err := buffered.Peek(zstd.HeaderMaxSize)
	if err != nil && !errors.Is(err, io.EOF) {
		return nil, 0, nil, fmt.Errorf("%w: read artifact header", errRetryableDownload)
	}
	if !bytes.HasPrefix(prefix, zstdMagic) {
		if contentLength < 0 {
			return nil, 0, nil, fmt.Errorf("%w: raw artifact has no content length", ErrImageIntegrity)
		}
		return buffered, contentLength, func() {}, nil
	}

	var header zstd.Header
	if err := header.Decode(prefix); err != nil || !header.HasFCS {
		return nil, 0, nil, fmt.Errorf("%w: zstd artifact has no content size", ErrImageIntegrity)
	}
	decoder, err := zstd.NewReader(buffered, zstd.WithDecoderConcurrency(snapshotEncoderConcurrency))
	if err != nil {
		return nil, 0, nil, fmt.Errorf("create artifact decoder: %w", err)
	}
	return decoder, int64(header.FrameContentSize), decoder.Close, nil
}

// temporaryFileDestination downloads into a temporary file in directory.
type temporaryFileDestination struct {
	directory string
	file      *os.File
}

func (destination *temporaryFileDestination) open(_ context.Context, _ int64) (io.Writer, error) {
	if err := os.MkdirAll(destination.directory, 0o755); err != nil {
		return nil, err
	}
	file, err := os.CreateTemp(destination.directory, "download-*")
	if err != nil {
		return nil, err
	}
	destination.file = file
	return file, nil
}

func (destination *temporaryFileDestination) commit() error {
	if err := destination.file.Sync(); err != nil {
		return err
	}
	return destination.file.Close()
}

func (destination *temporaryFileDestination) discard() {
	if destination.file == nil {
		return
	}
	destination.file.Close()
	os.Remove(destination.file.Name())
	destination.file = nil
}

// sparseVolumeWriter skips all-zero writes. It suits a new ZFS volume, which reads
// as zeros where nothing was written.
type sparseVolumeWriter struct {
	file   *os.File
	offset int64
}

var zeroBuffer = make([]byte, downloadBufferSize)

func (writer *sparseVolumeWriter) Write(data []byte) (int, error) {
	if len(data) > len(zeroBuffer) || !bytes.Equal(data, zeroBuffer[:len(data)]) {
		if _, err := writer.file.WriteAt(data, writer.offset); err != nil {
			return 0, err
		}
	}
	writer.offset += int64(len(data))
	return len(data), nil
}

// verifyFileSHA256 rereads a stored file and compares its digest.
func verifyFileSHA256(path, expectedDigest string) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()

	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return err
	}
	if hex.EncodeToString(hash.Sum(nil)) != expectedDigest {
		return fmt.Errorf("SHA-256 digest mismatch")
	}
	return nil
}

// parseImageURL accepts only an absolute http or https URL.
func parseImageURL(source string) (*url.URL, error) {
	parsed, err := url.ParseRequestURI(source)
	if err != nil || parsed.Host == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return nil, fmt.Errorf("invalid image URL")
	}
	return parsed, nil
}

// redactURL removes the query, fragment, and credentials, so a signed image URL
// can be logged without leaking the signature.
func redactURL(source string) string {
	parsed, err := url.Parse(source)
	if err != nil {
		return "invalid image URL"
	}
	parsed.RawQuery = ""
	parsed.Fragment = ""
	parsed.User = nil
	return parsed.String()
}
