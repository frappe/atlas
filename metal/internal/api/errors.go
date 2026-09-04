package api

import (
	"errors"
	"fmt"
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/network"
	"github.com/frappe/atlas/metal/internal/storage"
	"github.com/frappe/atlas/metal/internal/vm"
)

type errorBody struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type errorResponse struct {
	Error errorBody `json:"error"`
}

type apiError struct {
	status  int
	code    string
	message string
}

func (err *apiError) Error() string {
	return err.message
}

func errorHandler(err error, c echo.Context) {
	if c.Response().Committed {
		return
	}

	publicError := publicAPIError(err)
	_ = c.JSON(publicError.status, errorResponse{Error: errorBody{
		Code:    publicError.code,
		Message: publicError.message,
	}})
}

func publicAPIError(err error) *apiError {
	var explicitError *apiError
	if errors.As(err, &explicitError) {
		return explicitError
	}

	switch {
	case errors.Is(err, network.ErrInvalidPeers):
		return newAPIError(http.StatusBadRequest, "invalid_request", err.Error())
	case errors.Is(err, vm.ErrNotFound), errors.Is(err, storage.ErrNotFound):
		return newAPIError(http.StatusNotFound, "not_found", "resource not found")
	case errors.Is(err, storage.ErrImageConflict):
		return newAPIError(http.StatusConflict, "image_content_conflict", "image reference identifies different content")
	case errors.Is(err, vm.ErrConflict), errors.Is(err, storage.ErrInUse):
		return newAPIError(http.StatusConflict, "conflict", "resource conflict")
	case errors.Is(err, storage.ErrImageIntegrity):
		return newAPIError(http.StatusUnprocessableEntity, "image_integrity_failed", "image content failed verification")
	}

	var httpError *echo.HTTPError
	if errors.As(err, &httpError) {
		return newAPIError(httpError.Code, statusCode(httpError.Code), http.StatusText(httpError.Code))
	}
	return newAPIError(http.StatusInternalServerError, "internal_error", "internal server error")
}

func newAPIError(status int, code, message string) *apiError {
	return &apiError{status: status, code: code, message: message}
}

func statusCode(status int) string {
	switch status {
	case http.StatusBadRequest:
		return "invalid_request"
	case http.StatusUnauthorized:
		return "unauthorized"
	case http.StatusNotFound:
		return "not_found"
	case http.StatusConflict:
		return "conflict"
	case http.StatusNotImplemented:
		return "not_implemented"
	default:
		return fmt.Sprintf("http_%d", status)
	}
}

func badRequest(message string) error {
	return newAPIError(http.StatusBadRequest, "invalid_request", message)
}

func unauthorized() error {
	return newAPIError(http.StatusUnauthorized, "unauthorized", "invalid API token")
}
