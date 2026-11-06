package api

import (
	"fmt"
	"net/http"
	"strings"

	"github.com/labstack/echo/v5"
)

const maximumMetadataKeyLength = 128

// replaceVirtualMachineMetadataRequest carries the complete metadata map.
type replaceVirtualMachineMetadataRequest struct {
	Metadata map[string]string `json:"metadata"`
}

// @Summary	Replace virtual machine metadata
// @Description	Store the complete metadata map. Return 200 after immediate apply or 202 when reconciliation must continue.
// @ID			replaceVirtualMachineMetadata
// @Tags		Virtual machines
// @Accept		json
// @Produce	json
// @Param		id		path		string									true	"Virtual machine identifier"
// @Param		request	body		replaceVirtualMachineMetadataRequest	true	"Complete metadata map"
// @Success	200		{object}	virtualMachineResponse
// @Success	202		{object}	virtualMachineResponse
// @Failure	400		{object}	errorResponse
// @Failure	401		{object}	errorResponse
// @Failure	404		{object}	errorResponse
// @Failure	500		{object}	errorResponse
// @Router		/v1/vms/{id}/metadata [put]
func (s *Server) replaceVirtualMachineMetadata(c *echo.Context) error {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}

	var request replaceVirtualMachineMetadataRequest
	if err := decodeJSONRequest(c, &request); err != nil {
		return err
	}
	if err := validateMetadata(request.Metadata); err != nil {
		return badRequest(err.Error())
	}
	applied, err := s.virtualMachineManager.ReplaceMetadata(
		c.Request().Context(),
		identifier,
		request.Metadata,
	)
	if err != nil {
		return err
	}
	s.wakeReconciler()

	status := http.StatusAccepted
	if applied {
		status = http.StatusOK
	}
	return s.respondWithCurrentVirtualMachine(c, status)
}

// validateMetadata accepts an empty map to remove all metadata.
func validateMetadata(metadata map[string]string) error {
	for key := range metadata {
		if strings.TrimSpace(key) == "" {
			return fmt.Errorf("metadata key is empty")
		}
		if len(key) > maximumMetadataKeyLength {
			return fmt.Errorf("metadata key %q is too long", key)
		}
	}

	return nil
}
