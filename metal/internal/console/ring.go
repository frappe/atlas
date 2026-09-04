package console

// ringBuffer keeps the most recent console bytes for scrollback.
type ringBuffer struct {
	data  []byte
	start int
	size  int
}

func newRingBuffer(capacity int) *ringBuffer {
	if capacity <= 0 {
		capacity = 1
	}
	return &ringBuffer{data: make([]byte, capacity)}
}

func (r *ringBuffer) write(chunk []byte) {
	capacity := len(r.data)
	// Keep only the tail when the chunk exceeds capacity.
	if len(chunk) >= capacity {
		copy(r.data, chunk[len(chunk)-capacity:])
		r.start = 0
		r.size = capacity
		return
	}

	for _, value := range chunk {
		end := (r.start + r.size) % capacity
		r.data[end] = value
		if r.size < capacity {
			r.size++
		} else {
			r.start = (r.start + 1) % capacity
		}
	}
}

func (r *ringBuffer) snapshot() []byte {
	result := make([]byte, r.size)
	for index := 0; index < r.size; index++ {
		result[index] = r.data[(r.start+index)%len(r.data)]
	}
	return result
}
