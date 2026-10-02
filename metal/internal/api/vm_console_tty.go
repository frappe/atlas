package api

import (
	"context"
	"errors"

	"github.com/coder/websocket"

	"github.com/frappe/atlas/metal/internal/console"
)

// streamTTYConsole joins the viewer to the VM's shared serial console. The
// console keeps running when the viewer leaves, and other viewers are unaffected.
// The viewer stays connected while the VM restarts.
func (s *Server) streamTTYConsole(ctx context.Context, connection *websocket.Conn, id string) {
	terminal := newWebSocketTerminal(ctx, connection)

	err := s.serialBroker.Attach(ctx, id, terminal, terminal.resize)
	switch {
	case errors.Is(err, console.ErrConsoleNotFound):
		connection.Close(websocket.StatusGoingAway, "console unavailable")
	case errors.Is(err, console.ErrConsoleBusy):
		connection.Close(websocket.StatusTryAgainLater, "console has too many viewers")
	default:
		connection.Close(websocket.StatusNormalClosure, "")
	}
}
