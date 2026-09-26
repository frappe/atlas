package storage

import (
	"crypto/sha256"
	"io"
)

const (
	// Four 1 MiB buffers let reading stay ahead of hashing.
	sha256BufferCount = 4
	sha256BufferSize  = 1 << 20
)

// sha256Reader hashes the bytes it reads on a separate goroutine, so hashing
// runs beside compression instead of before it.
type sha256Reader struct {
	reader        io.Reader
	readBytes     int64
	filledBuffers chan []byte
	freeBuffers   chan []byte
	sum           chan []byte
}

func newSHA256Reader(source io.Reader) *sha256Reader {
	reader := &sha256Reader{
		reader:        source,
		filledBuffers: make(chan []byte, sha256BufferCount),
		freeBuffers:   make(chan []byte, sha256BufferCount),
		sum:           make(chan []byte, 1),
	}
	for range sha256BufferCount {
		reader.freeBuffers <- make([]byte, 0, sha256BufferSize)
	}
	go reader.hashBuffers()
	return reader
}

func (reader *sha256Reader) Read(buffer []byte) (int, error) {
	read, err := reader.reader.Read(buffer)
	reader.readBytes += int64(read)
	for data := buffer[:read]; len(data) > 0; {
		chunk := <-reader.freeBuffers
		size := min(len(data), cap(chunk))
		reader.filledBuffers <- append(chunk, data[:size]...)
		data = data[size:]
	}
	return read, err
}

// Sum waits for the hash goroutine and returns the digest. Call it once.
func (reader *sha256Reader) Sum() []byte {
	close(reader.filledBuffers)
	return <-reader.sum
}

func (reader *sha256Reader) hashBuffers() {
	hash := sha256.New()
	for chunk := range reader.filledBuffers {
		hash.Write(chunk)
		reader.freeBuffers <- chunk[:0]
	}
	reader.sum <- hash.Sum(nil)
}
