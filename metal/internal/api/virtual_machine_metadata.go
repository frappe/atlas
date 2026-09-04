package api

import (
	"fmt"
	"net/http"
	"strings"

	"github.com/labstack/echo/v4"
)

const (
	maximumMetadataCount       = 64
	maximumMetadataKeyLength   = 128
	maximumMetadataValueLength = 1024
)

type replaceVirtualMachineMetadataRequest struct {
	Metadata map[string]string `json:"metadata"`
}

// @Summary	Replace virtual machine metadata
// @Tags		vms
// @Accept		json
// @Produce	json
// @Param		id		path		string									true	"Virtual machine identifier"
// @Param		body	body		replaceVirtualMachineMetadataRequest	true	"Complete metadata map"
// @Success	200		{object}	virtualMachineResponse
// @Failure	400		{object}	errorResponse
// @Failure	404		{object}	errorResponse
// @Router		/vms/{id}/metadata [put]
func (s *Server) replaceVirtualMachineMetadata(c echo.Context) error {
	virtualMachineID := c.Param("id")
	if !validResourceID(virtualMachineID) {
		return badRequest("invalid virtual machine identifier")
	}

	var request replaceVirtualMachineMetadataRequest
	if err := c.Bind(&request); err != nil {
		return badRequest("invalid JSON request")
	}
	if err := validateMetadata(request.Metadata); err != nil {
		return badRequest(err.Error())
	}
	if err := s.virtualMachineDriver.ReplaceMetadata(
		c.Request().Context(),
		virtualMachineID,
		request.Metadata,
	); err != nil {
		return err
	}

	virtualMachine, err := s.loadVirtualMachine(c)
	if err != nil {
		return err
	}
	return s.respondWithVirtualMachine(c, http.StatusOK, virtualMachine)
}

// validateMetadata checks a plain string-to-string metadata map. An empty or nil
// map is valid and means no metadata.
func validateMetadata(metadata map[string]string) error {
	if len(metadata) > maximumMetadataCount {
		return fmt.Errorf("metadata cannot contain more than %d entries", maximumMetadataCount)
	}

	for key, value := range metadata {
		if strings.TrimSpace(key) == "" {
			return fmt.Errorf("metadata key is empty")
		}
		if len(key) > maximumMetadataKeyLength {
			return fmt.Errorf("metadata key %q is too long", key)
		}
		if len(value) > maximumMetadataValueLength {
			return fmt.Errorf("metadata %q value is too long", key)
		}
	}

	return nil
}
