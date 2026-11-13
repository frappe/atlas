package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"

	"github.com/klauspost/compress/zstd"
)

// buildResult is what Atlas reads after a build. It holds an error or the digest
// and size of the raw file system, which hosts verify after they decompress it.
type buildResult struct {
	RootfsSHA256    string `json:"rootfs_sha256,omitempty"`
	RootfsSizeBytes int64  `json:"rootfs_size_bytes,omitempty"`
	Error           string `json:"error,omitempty"`
}

// upload sends the zstd copy of the file, the format that hosts download.
func upload(ctx context.Context, path, uploadURL string) (buildResult, error) {
	source, err := os.Open(path)
	if err != nil {
		return buildResult{}, err
	}
	defer source.Close()

	compressedPath := path + ".zst"
	compressed, err := os.Create(compressedPath)
	if err != nil {
		return buildResult{}, err
	}
	defer os.Remove(compressedPath)
	defer compressed.Close()

	information, err := source.Stat()
	if err != nil {
		return buildResult{}, err
	}
	encoder, err := zstd.NewWriter(nil)
	if err != nil {
		return buildResult{}, err
	}
	// A host refuses a zstd frame that does not state its content size.
	encoder.ResetContentSize(compressed, information.Size())
	digest := sha256.New()
	size, err := io.Copy(encoder, io.TeeReader(source, digest))
	if err != nil {
		return buildResult{}, err
	}
	if err := encoder.Close(); err != nil {
		return buildResult{}, err
	}

	if _, err := compressed.Seek(0, io.SeekStart); err != nil {
		return buildResult{}, err
	}
	if err := put(ctx, uploadURL, compressed); err != nil {
		return buildResult{}, fmt.Errorf("upload the file system: %w", err)
	}
	return buildResult{RootfsSHA256: hex.EncodeToString(digest.Sum(nil)), RootfsSizeBytes: size}, nil
}

func putResult(ctx context.Context, resultURL string, result buildResult) error {
	data, err := json.Marshal(result)
	if err != nil {
		return err
	}
	return put(ctx, resultURL, bytes.NewReader(data))
}

// put sends one object. http.NewRequest reads the length of a bytes.Reader, and
// the file case sets it, because object storage refuses chunked uploads.
func put(ctx context.Context, address string, body io.ReadSeeker) error {
	length, err := body.Seek(0, io.SeekEnd)
	if err != nil {
		return err
	}
	if _, err := body.Seek(0, io.SeekStart); err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPut, address, body)
	if err != nil {
		return err
	}
	request.ContentLength = length

	response, err := http.DefaultClient.Do(request)
	if err != nil {
		// The URL error holds the signature, and Atlas shows this text to the tenant.
		var urlError *url.Error
		if errors.As(err, &urlError) {
			return urlError.Err
		}
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("status %d", response.StatusCode)
	}
	return nil
}
