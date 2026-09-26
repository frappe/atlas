package api

import (
	"net/http"

	"github.com/labstack/echo/v4"

	"github.com/frappe/atlas/metal/internal/storage"
)

// snapshotArtifactResponse is the exact size of one staged artifact. The
// controller needs it to plan a multipart upload.
type snapshotArtifactResponse struct {
	SizeBytes int64 `json:"size_bytes"`
}

// snapshotCreatedResponse identifies a new staged snapshot and its artifacts.
type snapshotCreatedResponse struct {
	ID     string                   `json:"id"`
	Rootfs snapshotArtifactResponse `json:"rootfs"`
	Kernel snapshotArtifactResponse `json:"kernel"`
}

// snapshotUploadPartRequest is one presigned destination supplied by the controller.
type snapshotUploadPartRequest struct {
	PartNumber int    `json:"part_number"`
	URL        string `json:"url"`
}

// snapshotArtifactUploadRequest carries every part of one artifact. The upload
// ID identifies the multipart upload the parts belong to, so a resumed upload
// never reuses an ETag from an upload the controller replaced.
type snapshotArtifactUploadRequest struct {
	UploadID    string                      `json:"upload_id"`
	PartSizeMiB int64                       `json:"part_size_mib"`
	Parts       []snapshotUploadPartRequest `json:"parts"`
}

// snapshotUploadRequest carries the parts of both artifacts.
type snapshotUploadRequest struct {
	Rootfs snapshotArtifactUploadRequest `json:"rootfs"`
	Kernel snapshotArtifactUploadRequest `json:"kernel"`
}

// uploadedPartResponse is the ETag the destination returned for one part.
type uploadedPartResponse struct {
	PartNumber int    `json:"part_number"`
	ETag       string `json:"etag"`
}

// uploadedArtifactResponse describes one finished artifact upload. SizeBytes is
// the uncompressed image and StoredSizeBytes is what the object store holds.
type uploadedArtifactResponse struct {
	SizeBytes       int64                  `json:"size_bytes"`
	StoredSizeBytes int64                  `json:"stored_size_bytes"`
	SHA256          string                 `json:"sha256"`
	Parts           []uploadedPartResponse `json:"parts"`
}

// snapshotStatusResponse reports upload progress. Artifact details appear only
// after the upload completes.
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
// @Description	Create local root file system and kernel artifacts from one virtual machine.
// @ID			createVirtualMachineSnapshot
// @Tags		Snapshots
// @Produce	json
// @Param		id			path		string	true	"Virtual machine identifier"
// @Success	201	{object}	snapshotCreatedResponse
// @Header		201	{string}	Location	"Path of the created snapshot"
// @Failure	400	{object}	errorResponse
// @Failure	401	{object}	errorResponse
// @Failure	404	{object}	errorResponse
// @Failure	409	{object}	errorResponse
// @Failure	500	{object}	errorResponse
// @Router		/v1/vms/{id}/snapshots [post]
func (s *Server) createVirtualMachineSnapshot(c echo.Context) error {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}

	snapshot, err := s.virtualMachineManager.CreateSnapshot(c.Request().Context(), identifier)
	if err != nil {
		return err
	}
	c.Response().Header().Set(echo.HeaderLocation, "/v1/snapshots/"+snapshot.ID)

	return c.JSON(http.StatusCreated, snapshotCreatedResponse{
		ID:     snapshot.ID,
		Rootfs: snapshotArtifactResponse{SizeBytes: snapshot.RootfsSizeBytes},
		Kernel: snapshotArtifactResponse{SizeBytes: snapshot.KernelSizeBytes},
	})
}

// @Summary	Start an image staging snapshot upload
// @Description	Start asynchronous multipart uploads for the staged root file system and kernel.
// @ID			uploadSnapshot
// @Tags		Snapshots
// @Accept		json
// @Param		id		path	string				true	"Snapshot identifier"
// @Param		request	body	snapshotUploadRequest	true	"Multipart upload URLs"
// @Success	202		"Accepted"
// @Failure	400		{object}	errorResponse
// @Failure	503		{object}	errorResponse
// @Failure	401		{object}	errorResponse
// @Failure	404		{object}	errorResponse
// @Failure	500		{object}	errorResponse
// @Router		/v1/snapshots/{id}/upload [post]
func (s *Server) uploadSnapshot(c echo.Context) error {
	identifier, err := snapshotID(c)
	if err != nil {
		return err
	}

	var request snapshotUploadRequest
	if err := decodeJSONRequest(c, &request); err != nil {
		return err
	}
	if err := s.snapshotStore.StartUpload(c.Request().Context(), identifier, request.storageRequest()); err != nil {
		return err
	}
	return c.NoContent(http.StatusAccepted)
}

// @Summary	Get an image staging snapshot upload status
// @Description	Return upload progress or the completed artifact details for one snapshot.
// @ID			getSnapshot
// @Tags		Snapshots
// @Produce	json
// @Param		id	path		string	true	"Snapshot identifier"
// @Success	200	{object}	snapshotStatusResponse
// @Failure	400	{object}	errorResponse
// @Failure	401	{object}	errorResponse
// @Failure	404	{object}	errorResponse
// @Failure	500	{object}	errorResponse
// @Router		/v1/snapshots/{id} [get]
func (s *Server) getSnapshot(c echo.Context) error {
	identifier, err := snapshotID(c)
	if err != nil {
		return err
	}
	status, err := s.snapshotStore.UploadStatus(c.Request().Context(), identifier)
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

// progressPercent reports upload progress, and clamps it so an over-count from a
// retried part cannot exceed 100.
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
// @Description	Remove local staging data for one snapshot.
// @ID			deleteSnapshot
// @Tags		Snapshots
// @Param		id	path	string	true	"Snapshot identifier"
// @Success	204	"No content"
// @Failure	400	{object}	errorResponse
// @Failure	401	{object}	errorResponse
// @Failure	500	{object}	errorResponse
// @Router		/v1/snapshots/{id} [delete]
func (s *Server) deleteSnapshot(c echo.Context) error {
	identifier, err := snapshotID(c)
	if err != nil {
		return err
	}
	if err := s.snapshotStore.DeleteSnapshot(c.Request().Context(), identifier); err != nil {
		return err
	}
	return c.NoContent(http.StatusNoContent)
}

// storageRequest converts the request into the storage form.
func (request snapshotUploadRequest) storageRequest() storage.SnapshotUploadRequest {
	return storage.SnapshotUploadRequest{
		Rootfs: request.Rootfs.storageUpload(),
		Kernel: request.Kernel.storageUpload(),
	}
}

// storageUpload converts one artifact into the storage form.
func (request snapshotArtifactUploadRequest) storageUpload() storage.SnapshotArtifactUpload {
	return storage.SnapshotArtifactUpload{
		UploadID:      request.UploadID,
		PartSizeBytes: request.PartSizeMiB << 20,
		Parts:         storageParts(request.Parts),
	}
}

// storageParts converts the parts of one artifact into the storage form.
func storageParts(parts []snapshotUploadPartRequest) []storage.SnapshotUploadPart {
	result := make([]storage.SnapshotUploadPart, 0, len(parts))
	for _, part := range parts {
		result = append(result, storage.SnapshotUploadPart{PartNumber: part.PartNumber, URL: part.URL})
	}
	return result
}

// uploadedArtifact converts one finished artifact into the response form.
func uploadedArtifact(artifact storage.UploadedArtifact) uploadedArtifactResponse {
	parts := make([]uploadedPartResponse, 0, len(artifact.Parts))
	for _, part := range artifact.Parts {
		parts = append(parts, uploadedPartResponse{PartNumber: part.PartNumber, ETag: part.ETag})
	}
	return uploadedArtifactResponse{
		SizeBytes:       artifact.SizeBytes,
		StoredSizeBytes: artifact.StoredSizeBytes,
		SHA256:          artifact.SHA256,
		Parts:           parts,
	}
}
