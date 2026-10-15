package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"regexp"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/telemetry"
)

// correlationKey is a private context key type, so no other package can collide
// with these values.
type correlationKey string

// An operation ID groups the host work that one request starts. The request ID
// lives in telemetry, so the VM manager can name the request that changed a VM.
const operationIDKey correlationKey = "operation_id"

// safeCorrelationID accepts only values that are safe to echo in a response
// header and a log line. A caller-supplied ID is otherwise untrusted input.
var safeCorrelationID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)

// withCorrelation attaches both IDs to a request context.
func withCorrelation(requestContext context.Context, requestID, operationID string) context.Context {
	requestContext = telemetry.WithRequestID(requestContext, requestID)
	return context.WithValue(requestContext, operationIDKey, operationID)
}

// requestID returns the ID that identifies one HTTP request.
func requestID(requestContext context.Context) string {
	return telemetry.RequestID(requestContext)
}

// operationID returns the ID that groups the work one request starts.
func operationID(requestContext context.Context) string {
	value, _ := requestContext.Value(operationIDKey).(string)
	return value
}

// correlationMiddleware gives every request a request ID and an operation ID,
// keeps a valid caller-supplied value, and returns both in response headers.
func correlationMiddleware(next echo.HandlerFunc) echo.HandlerFunc {
	return func(c echo.Context) error {
		requestID := correlationHeader(c.Request(), "X-Request-ID")
		if requestID == "" {
			requestID = newCorrelationID()
		}
		operationID := correlationHeader(c.Request(), "X-Operation-ID")
		if operationID == "" {
			operationID = newCorrelationID()
		}
		c.SetRequest(c.Request().WithContext(withCorrelation(c.Request().Context(), requestID, operationID)))
		c.Response().Header().Set("X-Request-ID", requestID)
		c.Response().Header().Set("X-Operation-ID", operationID)
		return next(c)
	}
}

// correlationHeader returns a caller-supplied ID, or "" when it is unusable.
func correlationHeader(request *http.Request, name string) string {
	value := request.Header.Get(name)
	if safeCorrelationID.MatchString(value) {
		return value
	}
	return ""
}

// newCorrelationID returns a random ID.
func newCorrelationID() string {
	value := make([]byte, 16)
	if _, err := rand.Read(value); err != nil {
		return "unknown"
	}
	return hex.EncodeToString(value)
}
