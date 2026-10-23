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
	return downloadParts(ctx, client, []string{source}, expectedDigest, destination, loggers...)
}

// downloadParts fetches one artifact stored as consecutive objects. An object
// store limits the size of one upload, so a large artifact has several parts.
func downloadParts(ctx context.Context, client *http.Client, sources []string, expectedDigest string, destination downloadDestination, loggers ...*slog.Logger) error {
	logger := slog.Default()
	if len(loggers) > 0 && loggers[0] != nil {
		logger = loggers[0]
	}
	if len(sources) == 0 {
		return fmt.Errorf("no artifact URL")
	}
	for _, source := range sources {
		if _, err := parseImageURL(source); err != nil {
			return err
		}
	}

	redactedSource := redactURL(sources[0])

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
		err := downloadOnce(ctx, client, sources, expectedDigest, destination)
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
func downloadOnce(ctx context.Context, client *http.Client, sources []string, expectedDigest string, destination downloadDestination) error {
	parts := &partsBody{ctx: ctx, client: client, sources: sources}
	defer parts.close()
	contentLength, err := parts.openNext()
	if err != nil {
		return err
	}
	if len(sources) > 1 {
		contentLength = -1
	}

	body, sizeBytes, closeBody, err := decompressedBody(parts, contentLength)
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
	if reader.readBytes != sizeBytes {
		return fmt.Errorf("%w: decoded %d of %d artifact bytes", ErrImageIntegrity, reader.readBytes, sizeBytes)
	}
	if hex.EncodeToString(digest) != expectedDigest {
		return fmt.Errorf("%w: SHA-256 digest mismatch", ErrImageIntegrity)
	}
	return destination.commit()
}

// partsBody reads the objects of one artifact in order as one stream.
type partsBody struct {
	ctx     context.Context
	client  *http.Client
	sources []string
	current io.ReadCloser
	// remaining counts the bytes the current object still owes, or is -1 when unknown.
	remaining int64
}

func (body *partsBody) openNext() (int64, error) {
	request, err := http.NewRequestWithContext(body.ctx, http.MethodGet, body.sources[0], nil)
	if err != nil {
		return 0, fmt.Errorf("create request")
	}
	result, err := body.client.Do(request)
	if err != nil {
		if body.ctx.Err() != nil {
			return 0, body.ctx.Err()
		}
		return 0, fmt.Errorf("%w: request failed", errRetryableDownload)
	}
	if result.StatusCode != http.StatusOK {
		result.Body.Close()
		statusError := fmt.Errorf("HTTP status %d", result.StatusCode)
		if result.StatusCode == http.StatusRequestTimeout || result.StatusCode == http.StatusTooManyRequests || result.StatusCode >= 500 {
			return 0, fmt.Errorf("%w: %v", errRetryableDownload, statusError)
		}
		return 0, statusError
	}
	body.sources = body.sources[1:]
	body.current = result.Body
	body.remaining = result.ContentLength
	return result.ContentLength, nil
}

func (body *partsBody) Read(buffer []byte) (int, error) {
	for {
		if body.current == nil {
			if len(body.sources) == 0 {
				return 0, io.EOF
			}
			if _, err := body.openNext(); err != nil {
				return 0, err
			}
		}
		read, err := body.current.Read(buffer)
		if body.remaining >= 0 {
			body.remaining -= int64(read)
		}
		if !errors.Is(err, io.EOF) {
			return read, err
		}
		if body.remaining > 0 {
			return read, fmt.Errorf("truncated response")
		}
		body.close()
		if read > 0 {
			return read, nil
		}
	}
}

func (body *partsBody) close() {
	if body.current != nil {
		body.current.Close()
		body.current = nil
	}
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

// SparseWriter skips all-zero writes. It suits a new file or ZFS volume, which
// reads as zeros where nothing was written.
type SparseWriter struct {
	File   *os.File
	offset int64
}

var zeroBuffer = make([]byte, downloadBufferSize)

// Write writes data at the next offset and skips an all-zero chunk. The caller sets the final file length.
func (writer *SparseWriter) Write(data []byte) (int, error) {
	if len(data) > len(zeroBuffer) || !bytes.Equal(data, zeroBuffer[:len(data)]) {
		if _, err := writer.File.WriteAt(data, writer.offset); err != nil {
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
