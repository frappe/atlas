package telemetry

import "context"

type requestIDKey struct{}

// WithRequestID returns ctx carrying the ID of the API request that started
// the work, so a later log line can name the request that caused it.
func WithRequestID(ctx context.Context, requestID string) context.Context {
	return context.WithValue(ctx, requestIDKey{}, requestID)
}

// RequestID returns the API request ID in ctx, or "".
func RequestID(ctx context.Context) string {
	value, _ := ctx.Value(requestIDKey{}).(string)
	return value
}
