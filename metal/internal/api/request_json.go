package api

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"

	"github.com/labstack/echo/v4"
)

// maximumJSONBodyBytes caps a request body, so a large upload cannot exhaust
// host memory before it is rejected.
const maximumJSONBodyBytes = 1 << 20

// decodeJSONRequest decodes one JSON body and rejects trailing values. Unknown
// fields are ignored, so Atlas can add an optional field before every host upgrades.
func decodeJSONRequest(c echo.Context, value any) error {
	request := c.Request()
	request.Body = http.MaxBytesReader(c.Response(), request.Body, maximumJSONBodyBytes)
	decoder := json.NewDecoder(request.Body)
	if err := decoder.Decode(value); err != nil {
		return badRequest("invalid JSON request")
	}

	var trailing any
	err := decoder.Decode(&trailing)
	if !errors.Is(err, io.EOF) {
		return badRequest("invalid JSON request")
	}
	return nil
}

// decodeOptionalJSON accepts an empty body and leaves the value unchanged.
func decodeOptionalJSON(c echo.Context, value any) error {
	request := c.Request()
	request.Body = http.MaxBytesReader(c.Response(), request.Body, maximumJSONBodyBytes)
	data, err := io.ReadAll(request.Body)
	if err != nil {
		return badRequest("invalid JSON request")
	}
	if len(bytes.TrimSpace(data)) == 0 {
		return nil
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	if err := decoder.Decode(value); err != nil {
		return badRequest("invalid JSON request")
	}

	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return badRequest("invalid JSON request")
	}
	return nil
}
