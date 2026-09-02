package api

import (
	"errors"
	"fmt"
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/vm"
)

// errorHandler is the router's single error handler. It maps a driver sentinel
// or an echo.HTTPError to a status code and writes the uniform error body
// {"error":{"message": ...}}.
func errorHandler(err error, c echo.Context) {
	if c.Response().Committed {
		return
	}
	status, msg := http.StatusInternalServerError, err.Error()
	switch {
	case errors.Is(err, vm.ErrNotFound):
		status = http.StatusNotFound
	case errors.Is(err, vm.ErrConflict):
		status = http.StatusConflict
	default:
		if he, ok := errors.AsType[*echo.HTTPError](err); ok {
			status = he.Code
			msg = fmt.Sprint(he.Message)
		}
	}
	_ = c.JSON(status, echo.Map{"error": echo.Map{"message": msg}})
}

// badRequest returns a 400 carrying msg.
func badRequest(msg string) error {
	return echo.NewHTTPError(http.StatusBadRequest, msg)
}

// notImplemented handles a specced-but-unbuilt route with a 501.
func notImplemented(c echo.Context) error {
	return echo.NewHTTPError(http.StatusNotImplemented, "not implemented")
}
