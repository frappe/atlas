package console

import (
	"bytes"
	"sync"
)

// fakeClient records output and blocks on Read during tests.
type fakeClient struct {
	mutex   sync.Mutex
	buffer  bytes.Buffer
	release chan struct{}
}

func newFakeClient() *fakeClient {
	return &fakeClient{release: make(chan struct{})}
}

func (client *fakeClient) Write(data []byte) (int, error) {
	client.mutex.Lock()
	defer client.mutex.Unlock()
	return client.buffer.Write(data)
}

func (client *fakeClient) Read([]byte) (int, error) {
	<-client.release
	return 0, nil
}

func (client *fakeClient) written() []byte {
	client.mutex.Lock()
	defer client.mutex.Unlock()
	return append([]byte(nil), client.buffer.Bytes()...)
}
