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

	// downloadBufferSize sets the write size of an uncompressed artifact. io.Copy
	// writes through this buffer, so a small one costs one syscall per few bytes.
	downloadBufferSize = 1 << 20
)

// download fetches one artifact into directory and returns its temporary path.
// The caller renames or imports the file. Retries use exponential backoff, and
// the URL is redacted in logs because it carries a signature.
func download(ctx context.Context, client *http.Client, directory, source, expectedDigest string, loggers ...*slog.Logger) (string, error) {
	logger := slog.Default()
	if len(loggers) > 0 && loggers[0] != nil {
		logger = loggers[0]
	}
	if _, err := parseImageURL(source); err != nil {
		return "", err
	}
	if err := os.MkdirAll(directory, 0o755); err != nil {
		return "", err
	}

	redactedSource := redactURL(source)

	var lastError error
	for attempt := 1; attempt <= downloadAttempts; attempt++ {
		if attempt > 1 {
			delay := downloadBaseDelay << (attempt - 2)
			logger.Warn("image download failed, retrying", "source", redactedSource, "attempt", attempt-1, "maximum_attempts", downloadAttempts, "retry_after", delay, "error", lastError)
			select {
			case <-ctx.Done():
				return "", ctx.Err()
			case <-time.After(delay):
			}
		}
		path, err := downloadOnce(ctx, client, directory, source, expectedDigest)
		if err == nil {
			return path, nil
		}
		if !errors.Is(err, errRetryableDownload) {
			return "", fmt.Errorf("download %s: %w", redactedSource, err)
		}
		lastError = err
	}
	return "", fmt.Errorf("download %s after %d attempts: %w", redactedSource, downloadAttempts, lastError)
}

// downloadOnce makes one attempt and verifies the digest as it writes. A
// truncated body or a wrong digest removes the file instead of keeping it.
func downloadOnce(ctx context.Context, client *http.Client, directory, source, expectedDigest string) (string, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, source, nil)
	if err != nil {
		return "", fmt.Errorf("create request")
	}
	result, err := client.Do(request)
	if err != nil {
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		return "", fmt.Errorf("%w: request failed", errRetryableDownload)
	}
	defer result.Body.Close()
	if result.StatusCode != http.StatusOK {
		statusError := fmt.Errorf("HTTP status %d", result.StatusCode)
		if result.StatusCode == http.StatusRequestTimeout || result.StatusCode == http.StatusTooManyRequests || result.StatusCode >= 500 {
			return "", fmt.Errorf("%w: %v", errRetryableDownload, statusError)
		}
		return "", statusError
	}

	file, err := os.CreateTemp(directory, "download-*")
	if err != nil {
		return "", err
	}
	path := file.Name()
	remove := func() {
		file.Close()
		os.Remove(path)
	}

	// Count wire bytes. Compression makes them differ from bytes on disk.
	transferred := &countingReader{reader: result.Body, counter: new(atomic.Int64)}
	body, closeBody, err := decompressedBody(transferred)
	if err != nil {
		remove()
		return "", err
	}
	defer closeBody()

	hash := sha256.New()
	_, err = io.Copy(io.MultiWriter(file, hash), body)
	if err == nil && result.ContentLength >= 0 && transferred.counter.Load() != result.ContentLength {
		err = fmt.Errorf("truncated response")
	}
	if err == nil && hex.EncodeToString(hash.Sum(nil)) != expectedDigest {
		err = fmt.Errorf("%w: SHA-256 digest mismatch", ErrImageIntegrity)
	}
	if err != nil {
		remove()
		return "", err
	}
	if err := file.Sync(); err != nil {
		remove()
		return "", err
	}
	if err := file.Close(); err != nil {
		os.Remove(path)
		return "", err
	}

	return path, nil
}

// zstdMagic starts every zstd frame. A raw disk image never begins with it.
var zstdMagic = []byte{0x28, 0xB5, 0x2F, 0xFD}

// decompressedBody detects zstd content and returns decoded artifact bytes.
func decompressedBody(body io.Reader) (io.Reader, func(), error) {
	buffered := bufio.NewReaderSize(body, downloadBufferSize)
	prefix, err := buffered.Peek(len(zstdMagic))
	if err != nil && !errors.Is(err, io.EOF) {
		return nil, nil, fmt.Errorf("%w: read artifact header", errRetryableDownload)
	}
	if !bytes.Equal(prefix, zstdMagic) {
		return buffered, func() {}, nil
	}

	decoder, err := zstd.NewReader(buffered, zstd.WithDecoderConcurrency(snapshotEncoderConcurrency))
	if err != nil {
		return nil, nil, fmt.Errorf("create artifact decoder: %w", err)
	}
	return decoder, decoder.Close, nil
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
