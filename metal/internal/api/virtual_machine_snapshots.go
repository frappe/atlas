package api

import (
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/storage"
)

type snapshotArtifactResponse struct {
	SizeBytes int64 `json:"size_bytes"`
}

type snapshotCreatedResponse struct {
	ID     string                   `json:"id"`
	Rootfs snapshotArtifactResponse `json:"rootfs"`
	Kernel snapshotArtifactResponse `json:"kernel"`
}

type snapshotUploadPartRequest struct {
	PartNumber int    `json:"part_number"`
	URL        string `json:"url"`
}

type snapshotArtifactUploadRequest struct {
	Parts []snapshotUploadPartRequest `json:"parts"`
}

type snapshotUploadRequest struct {
	Rootfs snapshotArtifactUploadRequest `json:"rootfs"`
	Kernel snapshotArtifactUploadRequest `json:"kernel"`
}

type uploadedPartResponse struct {
	PartNumber int    `json:"part_number"`
	ETag       string `json:"etag"`
}

type uploadedArtifactResponse struct {
	SizeBytes int64                  `json:"size_bytes"`
	SHA256    string                 `json:"sha256"`
	Parts     []uploadedPartResponse `json:"parts"`
}

type snapshotStatusResponse struct {
	ID              string                    `json:"id"`
	State           string                    `json:"state"`
	UploadedBytes   int64                     `json:"uploaded_bytes"`
	TotalBytes      int64                     `json:"total_bytes"`
	ProgressPercent int                       `json:"progress_percent"`
	Rootfs          *uploadedArtifactResponse `json:"rootfs,omitempty"`
	Kernel          *uploadedArtifactResponse `json:"kernel,omitempty"`
	Error           string                    `json:"error,omitempty"`
}

// @Summary	Create an image staging snapshot
// @Tags		snapshots
// @Produce	json
// @Param		id			path		string	true	"Virtual machine identifier"
// @Success	201	{object}	snapshotCreatedResponse
// @Failure	400	{object}	errorResponse
// @Failure	404	{object}	errorResponse
// @Failure	409	{object}	errorResponse
// @Router		/vms/{id}/snapshots [post]
func (s *Server) createVirtualMachineSnapshot(c echo.Context) error {
	virtualMachineID := c.Param("id")
	if !validResourceID(virtualMachineID) {
		return badRequest("invalid virtual machine identifier")
	}

	snapshot, err := s.snapshotCreator.CreateSnapshot(c.Request().Context(), virtualMachineID)
	if err != nil {
		return err
	}
	return c.JSON(http.StatusCreated, snapshotCreatedResponse{
		ID:     snapshot.ID,
		Rootfs: snapshotArtifactResponse{SizeBytes: snapshot.Rootfs.SizeBytes},
		Kernel: snapshotArtifactResponse{SizeBytes: snapshot.Kernel.SizeBytes},
	})
}

// @Summary	Start an image staging snapshot upload
// @Tags		snapshots
// @Accept		json
// @Param		snapshot_id	path		string				true	"Snapshot identifier"
// @Param		body		body		snapshotUploadRequest	true	"Multipart upload URLs"
// @Success	202			"Accepted"
// @Failure	400			{object}	errorResponse
// @Failure	404			{object}	errorResponse
// @Router		/snapshots/{snapshot_id}/upload [post]
func (s *Server) uploadSnapshot(c echo.Context) error {
	snapshotID := c.Param("snapshot_id")
	if !validResourceID(snapshotID) {
		return badRequest("invalid snapshot identifier")
	}

	var request snapshotUploadRequest
	if err := c.Bind(&request); err != nil {
		return badRequest("invalid JSON request")
	}
	if err := s.snapshotStore.StartUpload(c.Request().Context(), snapshotID, request.storageRequest()); err != nil {
		return err
	}
	return c.NoContent(http.StatusAccepted)
}

// @Summary	Get an image staging snapshot upload status
// @Tags		snapshots
// @Produce	json
// @Param		snapshot_id	path		string	true	"Snapshot identifier"
// @Success	200			{object}	snapshotStatusResponse
// @Failure	400			{object}	errorResponse
// @Failure	404			{object}	errorResponse
// @Router		/snapshots/{snapshot_id} [get]
func (s *Server) getSnapshot(c echo.Context) error {
	snapshotID := c.Param("snapshot_id")
	if !validResourceID(snapshotID) {
		return badRequest("invalid snapshot identifier")
	}
	status, err := s.snapshotStore.UploadStatus(c.Request().Context(), snapshotID)
	if err != nil {
		return err
	}

	response := snapshotStatusResponse{
		ID:              status.ID,
		State:           status.State,
		UploadedBytes:   status.UploadedBytes,
		TotalBytes:      status.TotalBytes,
		ProgressPercent: progressPercent(status.UploadedBytes, status.TotalBytes),
		Error:           status.Error,
	}
	if status.State == storage.UploadStateCompleted {
		rootfs := uploadedArtifact(status.Result.Rootfs)
		kernel := uploadedArtifact(status.Result.Kernel)
		response.Rootfs = &rootfs
		response.Kernel = &kernel
	}
	return c.JSON(http.StatusOK, response)
}

func progressPercent(uploaded, total int64) int {
	if total <= 0 {
		return 0
	}
	if uploaded >= total {
		return 100
	}
	return int(uploaded * 100 / total)
}

// @Summary	Delete an image staging snapshot
// @Tags		snapshots
// @Param		snapshot_id	path	string	true	"Snapshot identifier"
// @Success	204			"No content"
// @Failure	400			{object}	errorResponse
// @Router		/snapshots/{snapshot_id} [delete]
func (s *Server) deleteSnapshot(c echo.Context) error {
	snapshotID := c.Param("snapshot_id")
	if !validResourceID(snapshotID) {
		return badRequest("invalid snapshot identifier")
	}
	if err := s.snapshotStore.DeleteSnapshot(c.Request().Context(), snapshotID); err != nil {
		return err
	}
	return c.NoContent(http.StatusNoContent)
}

func (request snapshotUploadRequest) storageRequest() storage.SnapshotUploadRequest {
	return storage.SnapshotUploadRequest{
		Rootfs: storage.SnapshotArtifactUpload{Parts: storageParts(request.Rootfs.Parts)},
		Kernel: storage.SnapshotArtifactUpload{Parts: storageParts(request.Kernel.Parts)},
	}
}

func storageParts(parts []snapshotUploadPartRequest) []storage.SnapshotUploadPart {
	result := make([]storage.SnapshotUploadPart, 0, len(parts))
	for _, part := range parts {
		result = append(result, storage.SnapshotUploadPart{PartNumber: part.PartNumber, URL: part.URL})
	}
	return result
}

func uploadedArtifact(artifact storage.UploadedArtifact) uploadedArtifactResponse {
	parts := make([]uploadedPartResponse, 0, len(artifact.Parts))
	for _, part := range artifact.Parts {
		parts = append(parts, uploadedPartResponse{PartNumber: part.PartNumber, ETag: part.ETag})
	}
	return uploadedArtifactResponse{SizeBytes: artifact.SizeBytes, SHA256: artifact.SHA256, Parts: parts}
}
