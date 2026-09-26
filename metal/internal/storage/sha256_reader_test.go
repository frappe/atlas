package storage

import (
	"bytes"
	"crypto/sha256"
	"io"
	"testing"
	"testing/iotest"
)

func TestSHA256ReaderReusesItsBuffersInOrder(t *testing.T) {
	contents := make([]byte, 64<<10)
	for index := range contents {
		contents[index] = byte(index * 31)
	}
	reader := newSHA256Reader(iotest.OneByteReader(bytes.NewReader(contents)))
	if _, err := io.Copy(io.Discard, reader); err != nil {
		t.Fatal(err)
	}

	sum := sha256.Sum256(contents)
	if !bytes.Equal(reader.Sum(), sum[:]) {
		t.Fatal("digest does not match the source")
	}
}
