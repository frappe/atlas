package api

import (
	"net/http"

	"github.com/frappe/atlas/metal/internal/vm"

	"github.com/labstack/echo/v4"
)

type rescueRequest struct {
	RecoveryGeneration uint64          `json:"recovery_generation,omitempty"`
	Access             vm.RescueAccess `json:"access,omitempty" swaggertype:"string"`
	AllowUnprotected   bool            `json:"allow_unprotected,omitempty"`
	Action             string          `json:"action,omitempty"`
	Enabled            *bool           `json:"enabled"`
	Image              *imageRequest   `json:"image,omitempty"`
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
	if err := request.validate(); err != nil {
		return err
	}
	switch {
	case *request.Enabled:
		err = s.virtualMachineManager.RequestRescue(c.Request().Context(), identifier, request.Image.specification(), request.Access)
	case request.Action == "":
		err = s.virtualMachineManager.RequestRescueExit(c.Request().Context(), identifier)
	default:
		err = s.virtualMachineManager.RequestRecoveryAction(c.Request().Context(), identifier, request.Action, request.RecoveryGeneration)
	}
	if err != nil {
		return err
	}
	s.wakeReconciler()
	return s.respondWithCurrentVirtualMachine(c, http.StatusAccepted)
}

func (request *rescueRequest) validate() error {
	if request.Enabled == nil {
		return badRequest("enabled is required")
	}
	if *request.Enabled {
		if request.Action != "" || request.RecoveryGeneration != 0 {
			return badRequest("action must be omitted when enabling rescue")
		}
		if err := request.validateAccess(); err != nil {
			return err
		}
		return request.validateImage()
	}
	return request.validateExit()
}

func (request rescueRequest) validateExit() error {
	if request.Image != nil {
		return badRequest("image must be omitted when disabling rescue")
	}
	if request.Access != "" || request.AllowUnprotected {
		return badRequest("access must be omitted when disabling rescue")
	}
	return request.validateAction()
}

func (request *rescueRequest) validateAccess() error {
	if request.Access == "" {
		request.Access = vm.RescueProtected
	}
	switch request.Access {
	case vm.RescueProtected, vm.RescueReadOnly, vm.RescueUnprotected:
	default:
		return badRequest("invalid rescue access")
	}
	if (request.Access == vm.RescueUnprotected) != request.AllowUnprotected {
		return badRequest("unprotected access requires explicit acknowledgement")
	}
	return nil
}

func (request rescueRequest) validateImage() error {
	if request.Image == nil {
		return badRequest("image is required when enabling rescue")
	}
	if err := request.Image.validate(); err != nil {
		return badRequest(err.Error())
	}
	if request.Image.MemorySnapshot || request.Image.MemorySnapshotConfiguration != nil {
		return badRequest("rescue requires a cold boot image")
	}
	return nil
}

func (request rescueRequest) validateAction() error {
	switch request.Action {
	case "":
		if request.RecoveryGeneration != 0 {
			return badRequest("recovery_generation requires a recovery action")
		}
	case "rollback", "finish":
		if request.RecoveryGeneration == 0 {
			return badRequest("recovery_generation is required for checkpoint actions")
		}
	default:
		return badRequest("invalid recovery action")
	}
	return nil
}
