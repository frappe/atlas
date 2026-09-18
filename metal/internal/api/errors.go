package api

import (
	"errors"
	"fmt"
	"log/slog"
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

// errorBody is the error object every failed response carries.
type errorBody struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable bool   `json:"retryable"`
	RequestID string `json:"request_id,omitempty"`
}

// errorResponse wraps errorBody, so a failure and a success never share a shape.
type errorResponse struct {
	Error errorBody `json:"error"`
}

// apiError is an error with a chosen status and public message. Anything else
// becomes a 500 with no detail.
type apiError struct {
	status  int
	code    string
	message string
}

// Error returns the public message.
func (err *apiError) Error() string {
	return err.message
}

// errorHandler writes the public error and logs the cause of a server fault.
func errorHandler(err error, c echo.Context) {
	if c.Response().Committed {
		return
	}

	publicError := publicAPIError(err)

	// A masked error tells the caller nothing, so log the cause under an ID that
	// the response carries back.
	requestID := requestID(c.Request().Context())
	if publicError.status >= http.StatusInternalServerError {
		logger, ok := c.Get("logger").(*slog.Logger)
		if !ok || logger == nil {
			logger = slog.Default()
		}
		logger.Error("API request failed", "method", c.Request().Method, "path", c.Path(), "request_id", requestID, "operation_id", operationID(c.Request().Context()), "error", err)
	}

	_ = c.JSON(publicError.status, errorResponse{Error: errorBody{
		Code:      publicError.code,
		Message:   publicError.message,
		Retryable: isRetryableStatus(publicError.status),
		RequestID: requestID,
	}})
}

// publicAPIError maps a domain error to a status and a safe message. An
// unrecognized error becomes an internal error, so no internal detail escapes.
func publicAPIError(err error) *apiError {
	var explicitError *apiError
	if errors.As(err, &explicitError) {
		return explicitError
	}

	switch {
	case errors.Is(err, network.ErrInvalidPeers), errors.Is(err, network.ErrInvalidUnicastPeers),
		errors.Is(err, storage.ErrInvalidUpload):
		return newAPIError(http.StatusBadRequest, "invalid_request", err.Error())
	case errors.Is(err, vm.ErrNotFound), errors.Is(err, storage.ErrNotFound):
		return newAPIError(http.StatusNotFound, "not_found", "resource not found")
	case errors.Is(err, storage.ErrImageConflict):
		return newAPIError(http.StatusConflict, "image_content_conflict", "image reference identifies different content")
	case errors.Is(err, vm.ErrConflict), errors.Is(err, storage.ErrInUse):
		return newAPIError(http.StatusConflict, "conflict", "resource conflict")
	case errors.Is(err, storage.ErrShuttingDown):
		return newAPIError(http.StatusServiceUnavailable, "unavailable", "host is shutting down")
	case errors.Is(err, storage.ErrImageIntegrity):
		return newAPIError(http.StatusUnprocessableEntity, "image_integrity_failed", "image content failed verification")
	}

	var httpError *echo.HTTPError
	if errors.As(err, &httpError) {
		return newAPIError(httpError.Code, statusCode(httpError.Code), http.StatusText(httpError.Code))
	}
	return newAPIError(http.StatusInternalServerError, "internal_error", "internal server error")
}

// newAPIError builds an error with an explicit status, code, and message.
func newAPIError(status int, code, message string) *apiError {
	return &apiError{status: status, code: code, message: message}
}

// statusCode names a status for the response body.
func statusCode(status int) string {
	switch status {
	case http.StatusBadRequest:
		return "invalid_request"
	case http.StatusUnauthorized:
		return "unauthorized"
	case http.StatusForbidden:
		return "forbidden"
	case http.StatusNotFound:
		return "not_found"
	case http.StatusConflict:
		return "conflict"
	case http.StatusNotImplemented:
		return "not_implemented"
	case http.StatusServiceUnavailable:
		return "unavailable"
	default:
		return fmt.Sprintf("http_%d", status)
	}
}

// isRetryableStatus reports whether the caller should try the request again.
// Not implemented is excluded: repeating it cannot change the answer.
func isRetryableStatus(status int) bool {
	return status == http.StatusRequestTimeout ||
		status == http.StatusTooManyRequests ||
		(status >= http.StatusInternalServerError && status != http.StatusNotImplemented)
}

// badRequest reports an invalid request with a caller-facing message.
func badRequest(message string) error {
	return newAPIError(http.StatusBadRequest, "invalid_request", message)
}

// unauthorized reports a missing or wrong API token.
func unauthorized() *apiError {
	return newAPIError(http.StatusUnauthorized, "unauthorized", "invalid API token")
}
