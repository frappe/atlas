package api

import (
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/vm/migration"
)

// errInvalidMigrationRequest reports a destination request missing a field.
var errInvalidMigrationRequest = errors.New("virtual_machine_id and source are required")

// createMigrationRequest is Atlas's destination-pull request.
type createMigrationRequest struct {
	VirtualMachineID string                  `json:"virtual_machine_id"`
	Source           string                  `json:"source"`
	Resize           *migrationResizeRequest `json:"resize,omitempty"`
}

// migrationResizeRequest is the CPU, memory, and disk size the destination gives the VM.
type migrationResizeRequest struct {
	CPUMillicores int `json:"cpu_millicores" minimum:"100" maximum:"32000"`
	MemoryMiB     int `json:"memory_mib" minimum:"1"`
	DiskMiB       int `json:"disk_mib" minimum:"1"`
}

// validate rejects a request missing a required field or with an unusable resize.
func (r createMigrationRequest) validate() error {
	if r.VirtualMachineID == "" || r.Source == "" {
		return errInvalidMigrationRequest
	}
	if r.Resize == nil {
		return nil
	}
	compute := computeRequest{CPUMillicores: r.Resize.CPUMillicores, MemoryMiB: r.Resize.MemoryMiB}
	if err := compute.validate(); err != nil {
		return fmt.Errorf("resize: %w", err)
	}
	if r.Resize.DiskMiB <= 0 {
		return errors.New("resize: disk_mib must be positive")
	}
	return nil
}

// resize converts the optional request resize into the migration shape.
func (r createMigrationRequest) resize() *migration.Resize {
	if r.Resize == nil {
		return nil
	}
	return &migration.Resize{
		CPUMillicores: r.Resize.CPUMillicores,
		MemoryMiB:     r.Resize.MemoryMiB,
		DiskMiB:       r.Resize.DiskMiB,
	}
}

// migrationResponse is the public migration view.
type migrationResponse struct {
	ID               string                  `json:"id"`
	VirtualMachineID string                  `json:"virtual_machine_id"`
	Status           string                  `json:"status"`
	Phase            string                  `json:"phase"`
	Transfers        []migrationTransfer     `json:"transfers,omitempty"`
	Error            *migrationErrorResponse `json:"error,omitempty"`
}

// migrationTransfer reports one migration data transfer.
type migrationTransfer struct {
	Sequence        int        `json:"sequence"`
	StartedAt       time.Time  `json:"started_at"`
	FinishedAt      *time.Time `json:"finished_at,omitempty"`
	DurationSeconds int        `json:"duration_seconds"`
	TransferredMiB  int        `json:"transferred_mib"`
	TotalMiB        int        `json:"total_mib"`
	ThroughputMiBps int        `json:"throughput_mibps,omitempty"`
	Completed       bool       `json:"completed"`
}

// migrationErrorResponse is safe migration error detail.
type migrationErrorResponse struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

// createMigration creates or resumes a destination migration and starts its worker.
func (s *Server) createMigration(c echo.Context) error {
	identifier, err := migrationIdentifier(c)
	if err != nil {
		return err
	}
	var request createMigrationRequest
	if err := decodeJSONRequest(c, &request); err != nil {
		return err
	}
	if err := request.validate(); err != nil {
		return badRequest(err.Error())
	}

	record, err := s.migrationManager.CreateDestination(c.Request().Context(), identifier, request.VirtualMachineID, request.Source, request.resize())
	if err != nil {
		return err
	}
	s.wakeReconciler()
	return c.JSON(http.StatusAccepted, toMigration(record))
}

// getMigration returns destination migration status.
func (s *Server) getMigration(c echo.Context) error {
	identifier, err := migrationIdentifier(c)
	if err != nil {
		return err
	}
	record, err := s.migrationManager.DestinationStatus(c.Request().Context(), identifier)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusOK, toMigration(record))
}

// finishMigration records the destination's finish request. Atlas calls it with the
// static token. The destination then destroys the source over the mesh.
func (s *Server) finishMigration(c echo.Context) error {
	identifier, err := migrationIdentifier(c)
	if err != nil {
		return err
	}
	if err := s.migrationManager.RequestFinish(c.Request().Context(), identifier); err != nil {
		return err
	}
	s.wakeReconciler()
	return c.NoContent(http.StatusAccepted)
}

// abortMigration removes an unfinished destination migration.
func (s *Server) abortMigration(c echo.Context) error {
	identifier, err := migrationIdentifier(c)
	if err != nil {
		return err
	}
	if err := s.migrationManager.AbortDestination(c.Request().Context(), identifier); err != nil {
		return err
	}
	s.wakeReconciler()
	return c.NoContent(http.StatusAccepted)
}

// toMigration maps migration progress to its public response.
func toMigration(record migration.DestinationProgress) migrationResponse {
	response := migrationResponse{
		ID:               record.ID,
		VirtualMachineID: record.VirtualMachineID,
		Status:           string(record.Status),
		Phase:            string(record.Phase),
	}
	for _, interval := range record.Intervals {
		var finishedAt *time.Time
		if !interval.FinishedAt.IsZero() {
			finishedAt = &interval.FinishedAt
		}
		response.Transfers = append(response.Transfers, migrationTransfer{
			Sequence:        interval.Sequence,
			StartedAt:       interval.StartedAt,
			FinishedAt:      finishedAt,
			DurationSeconds: interval.DurationSeconds,
			TransferredMiB:  bytesToMiB(interval.BytesTransferred),
			TotalMiB:        bytesToMiB(interval.TotalBytes),
			ThroughputMiBps: interval.ThroughputMiBps,
			Completed:       interval.Completed,
		})
	}
	if record.Error != nil {
		response.Error = &migrationErrorResponse{Code: record.Error.Code, Message: record.Error.Message}
	}
	return response
}

func bytesToMiB(bytes int64) int {
	const bytesPerMiB = 1024 * 1024

	if bytes <= 0 {
		return 0
	}
	mib := bytes / bytesPerMiB
	if bytes%bytesPerMiB != 0 {
		mib++
	}
	return int(mib)
}

// migrationIdentifier reads and validates the path migration ID.
func migrationIdentifier(c echo.Context) (string, error) {
	identifier := c.Param("id")
	if !validResourceID(identifier) {
		return "", badRequest("invalid migration identifier")
	}
	return identifier, nil
}
