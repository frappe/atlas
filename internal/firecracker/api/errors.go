package api

import (
	"encoding/json"
	"fmt"
	"net/http"
)

// Error is a non-2xx response from the firecracker API.
type Error struct {
	Status  int
	Message string
}

func (e *Error) Error() string {
	return fmt.Sprintf("firecracker api: %d: %s", e.Status, e.Message)
}

func decodeFault(resp *http.Response) error {
	var f struct {
		FaultMessage string `json:"fault_message"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&f)
	return &Error{Status: resp.StatusCode, Message: f.FaultMessage}
}
