package api

import (
	"context"
	"encoding/json"
	"errors"
	"io"

	"github.com/coder/websocket"
	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/console"
)

// consoleControlMessage carries a viewer request.
type consoleControlMessage struct {
	Resize *struct {
		Cols uint16 `json:"cols"`
		Rows uint16 `json:"rows"`
	} `json:"resize"`
}

// getVirtualMachineConsole upgrades to a websocket and streams a VM console.
func (s *Server) getVirtualMachineConsole(c echo.Context) error {
	id := c.Param("id")
	if !validResourceID(id) {
		return badRequest("invalid virtual machine identifier")
	}

	// Default options reject cross-origin browsers.
	connection, err := websocket.Accept(c.Response(), c.Request(), nil)
	if err != nil {
		return nil
	}
	defer connection.CloseNow()

	ctx, cancel := context.WithCancel(c.Request().Context())
	defer cancel()

	if c.QueryParam("mode") == "ssh" {
		s.streamSSHConsole(ctx, cancel, connection, id)
	} else {
		s.streamSerialConsole(ctx, connection, id)
	}
	return nil
}

// streamSerialConsole bridges the shared serial console to the websocket.
func (s *Server) streamSerialConsole(ctx context.Context, connection *websocket.Conn, id string) {
	inputReader, inputWriter := io.Pipe()
	resize := make(chan console.Winsize, 4)
	go readConsoleMessages(ctx, connection, inputWriter, resize)

	client := &consoleClient{ctx: ctx, connection: connection, input: inputReader}
	err := s.consoleBroker.Attach(ctx, id, client, resize)
	switch {
	case errors.Is(err, console.ErrConsoleNotFound):
		connection.Close(websocket.StatusGoingAway, "console unavailable")
	case errors.Is(err, console.ErrConsoleBusy):
		connection.Close(websocket.StatusTryAgainLater, "console has too many viewers")
	default:
		connection.Close(websocket.StatusNormalClosure, "")
	}
}

// streamSSHConsole bridges an interactive SSH session to the websocket.
func (s *Server) streamSSHConsole(ctx context.Context, cancel context.CancelFunc, connection *websocket.Conn, id string) {
	session, err := s.sshConnector.DialSSH(ctx, id)
	if err != nil {
		connection.Close(websocket.StatusGoingAway, "ssh session unavailable")
		return
	}
	defer session.Close()

	go func() {
		defer cancel()
		buffer := make([]byte, 32<<10)
		for {
			count, readErr := session.Read(buffer)
			if count > 0 {
				if connection.Write(ctx, websocket.MessageBinary, buffer[:count]) != nil {
					return
				}
			}
			if readErr != nil {
				return
			}
		}
	}()

	for {
		messageType, data, err := connection.Read(ctx)
		if err != nil {
			return
		}
		switch messageType {
		case websocket.MessageBinary:
			if _, err := session.Write(data); err != nil {
				return
			}
		case websocket.MessageText:
			var message consoleControlMessage
			if json.Unmarshal(data, &message) == nil && message.Resize != nil {
				_ = session.Resize(message.Resize.Cols, message.Resize.Rows)
			}
		}
	}
}

// readConsoleMessages forwards viewer input and resize requests.
func readConsoleMessages(ctx context.Context, connection *websocket.Conn, input *io.PipeWriter, resize chan<- console.Winsize) {
	for {
		messageType, data, err := connection.Read(ctx)
		if err != nil {
			_ = input.CloseWithError(err)
			return
		}
		switch messageType {
		case websocket.MessageBinary:
			if _, err := input.Write(data); err != nil {
				return
			}
		case websocket.MessageText:
			var message consoleControlMessage
			if json.Unmarshal(data, &message) == nil && message.Resize != nil {
				select {
				case resize <- console.Winsize{Rows: message.Resize.Rows, Cols: message.Resize.Cols}:
				default:
				}
			}
		}
	}
}

// consoleClient adapts a websocket connection to the console broker.
type consoleClient struct {
	ctx        context.Context
	connection *websocket.Conn
	input      *io.PipeReader
}

func (client *consoleClient) Read(buffer []byte) (int, error) {
	return client.input.Read(buffer)
}

func (client *consoleClient) Write(data []byte) (int, error) {
	if err := client.connection.Write(client.ctx, websocket.MessageBinary, data); err != nil {
		return 0, err
	}
	return len(data), nil
}
