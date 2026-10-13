package api

import (
	"context"
	"errors"

	"github.com/coder/websocket"

	"github.com/frappe/atlas/metal/internal/console"
	"github.com/frappe/atlas/metal/internal/vm"
)

// streamSSHConsole joins the viewer to an SSH session. The session ends when the
// viewer leaves or the guest shell exits. A metald shutdown keeps it, so the
// viewer can resume the same shell on the next metald.
func (s *Server) streamSSHConsole(ctx context.Context, connection *websocket.Conn, session vm.SSHSession) {
	terminal := newWebSocketTerminal(ctx, connection)
	err := session.Attach(ctx, terminal, terminal.resize)
	if s.consoleShutdown.Err() != nil {
		return
	}
	if errors.Is(err, console.ErrConsoleBusy) {
		connection.Close(websocket.StatusTryAgainLater, "console has too many viewers")
		return
	}

	_ = session.Close()
	connection.Close(websocket.StatusNormalClosure, "")
}
