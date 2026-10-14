package api

import (
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"strconv"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/vm/migration"
)

// prepareMigrationSource locks the source and returns its definition and state.
func (s *Server) prepareMigrationSource(c echo.Context) error {
	identifier, virtualMachineID, err := migrationSourceIdentifiers(c)
	if err != nil {
		return err
	}
	sourceDescription, err := s.migrationManager.LockSource(c.Request().Context(), identifier, virtualMachineID)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, sourceDescription)
}

// startMigrationStream starts a one-shot mutual-TLS listener for one ZFS stream.
func (s *Server) startMigrationStream(c echo.Context) error {
	identifier, virtualMachineID, err := migrationSourceIdentifiers(c)
	if err != nil {
		return err
	}
	var request migration.SnapshotStreamRequest
	if err := decodeJSONRequest(c, &request); err != nil {
		return err
	}
	if request.Sequence < 1 || request.ThroughputMiBps < 0 {
		return badRequest("invalid stream request")
	}
	if err := s.migrationManager.StartSourceStream(
		c.Request().Context(), identifier, virtualMachineID,
		request.Sequence, request.ResumeToken, request.ThroughputMiBps,
	); err != nil {
		return err
	}
	return c.NoContent(http.StatusAccepted)
}

// createMigrationSnapshot returns the next source snapshot.
func (s *Server) createMigrationSnapshot(c echo.Context) error {
	identifier, virtualMachineID, err := migrationSourceIdentifiers(c)
	if err != nil {
		return err
	}
	var request migration.SnapshotAcknowledgement
	if err := decodeOptionalJSON(c, &request); err != nil {
		return err
	}
	if request.ReceivedSequence < 0 {
		return badRequest("invalid received_sequence")
	}
	snapshot, err := s.migrationManager.NextSourceSnapshot(c.Request().Context(), identifier, virtualMachineID, request.ReceivedSequence)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, snapshot)
}

// stopMigrationSource stops the source, removes its network, and returns its
// final snapshot.
func (s *Server) stopMigrationSource(c echo.Context) error {
	identifier, virtualMachineID, err := migrationSourceIdentifiers(c)
	if err != nil {
		return err
	}
	var request migration.SnapshotAcknowledgement
	if err := decodeOptionalJSON(c, &request); err != nil {
		return err
	}
	if request.ReceivedSequence < 0 {
		return badRequest("invalid received_sequence")
	}
	snapshot, err := s.migrationManager.StopSource(c.Request().Context(), identifier, virtualMachineID, request.ReceivedSequence, request.HasMemorySnapshot)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, snapshot)
}

// streamMigrationMemorySnapshotFile streams one memory snapshot file over the node TLS listener.
func (s *Server) streamMigrationMemorySnapshotFile(c echo.Context) error {
	identifier, virtualMachineID, err := migrationSourceIdentifiers(c)
	if err != nil {
		return err
	}
	sequence, err := strconv.Atoi(c.QueryParam("sequence"))
	if err != nil || sequence < 1 {
		return badRequest("invalid sequence")
	}
	file, err := s.migrationManager.OpenSourceMemorySnapshotFile(c.Request().Context(), identifier, virtualMachineID, sequence, c.QueryParam("file"))
	if err != nil {
		return err
	}
	defer file.Close()

	c.Response().Header().Set("Content-Type", "application/octet-stream")
	c.Response().Header().Set("Trailer", "X-Snapshot-SHA256")
	hash := sha256.New()
	if _, err := io.Copy(io.MultiWriter(c.Response(), hash), file); err != nil {
		return err
	}
	c.Response().Header().Set("X-Snapshot-SHA256", hex.EncodeToString(hash.Sum(nil)))
	return nil
}

// startMigrationSource restores the source during rollback.
func (s *Server) startMigrationSource(c echo.Context) error {
	identifier, virtualMachineID, err := migrationSourceIdentifiers(c)
	if err != nil {
		return err
	}
	if err := s.migrationManager.StartSourceRollback(c.Request().Context(), identifier, virtualMachineID); err != nil {
		return err
	}
	return c.NoContent(http.StatusNoContent)
}

// destroyMigrationSource destroys the stopped source and its migration state.
func (s *Server) destroyMigrationSource(c echo.Context) error {
	identifier, virtualMachineID, err := migrationSourceIdentifiers(c)
	if err != nil {
		return err
	}
	if err := s.migrationManager.DestroySource(c.Request().Context(), identifier, virtualMachineID); err != nil {
		return err
	}
	return c.NoContent(http.StatusNoContent)
}

// deleteMigrationSource unlocks the source and removes migration state.
func (s *Server) deleteMigrationSource(c echo.Context) error {
	identifier, virtualMachineID, err := migrationSourceIdentifiers(c)
	if err != nil {
		return err
	}
	if err := s.migrationManager.UnlockSource(c.Request().Context(), identifier, virtualMachineID); err != nil {
		return err
	}
	return c.NoContent(http.StatusNoContent)
}

// migrationSourceIdentifiers reads the migration ID and the VM ID the destination
// sends. The destination and source hold the same VM ID across a migration.
func migrationSourceIdentifiers(c echo.Context) (string, string, error) {
	identifier, err := migrationIdentifier(c)
	if err != nil {
		return "", "", err
	}
	virtualMachineID := c.QueryParam("virtual_machine_id")
	if !validResourceID(virtualMachineID) {
		return "", "", badRequest("invalid virtual_machine_id")
	}
	return identifier, virtualMachineID, nil
}
