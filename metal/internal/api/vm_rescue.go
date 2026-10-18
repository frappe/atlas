package api

import (
	"net/http"

	"github.com/labstack/echo/v4"
)

type rescueRequest struct {
	Enabled *bool         `json:"enabled"`
	Image   *imageRequest `json:"image,omitempty"`
}

// @Summary Select the virtual machine rescue boot mode
// @Description Running VMs restart into the selected mode. Stopped VMs stay stopped. An active rescue session keeps its image and disk.
// @ID setVirtualMachineRescue
// @Tags Virtual machines
// @Accept json
// @Produce json
// @Param id path string true "Virtual machine identifier"
// @Param request body rescueRequest true "Rescue selection and image when enabling"
// @Success 202 {object} virtualMachineResponse
// @Failure 400 {object} errorResponse
// @Failure 401 {object} errorResponse
// @Failure 404 {object} errorResponse
// @Failure 409 {object} errorResponse
// @Failure 500 {object} errorResponse
// @Router /v1/vms/{id}/rescue [put]
func (s *Server) setVirtualMachineRescue(c echo.Context) error {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}
	var request rescueRequest
	if err := decodeJSONRequest(c, &request); err != nil {
		return err
	}
	if request.Enabled == nil {
		return badRequest("enabled is required")
	}
	if *request.Enabled {
		if request.Image == nil {
			return badRequest("image is required when enabling rescue")
		}
		if err := request.Image.validate(); err != nil {
			return badRequest(err.Error())
		}
		if request.Image.MemorySnapshot || request.Image.MemorySnapshotConfiguration != nil {
			return badRequest("rescue requires a cold boot image")
		}
		err = s.virtualMachineManager.RequestRescue(c.Request().Context(), identifier, request.Image.specification())
	} else {
		if request.Image != nil {
			return badRequest("image must be omitted when disabling rescue")
		}
		err = s.virtualMachineManager.RequestRescueExit(c.Request().Context(), identifier)
	}
	if err != nil {
		return err
	}
	s.wakeReconciler()
	return s.respondWithCurrentVirtualMachine(c, http.StatusAccepted)
}
