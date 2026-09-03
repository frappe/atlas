package api

import (
	"bytes"
	"fmt"
	"net/http"
	"strings"

	"github.com/labstack/echo/v4"
	"golang.org/x/crypto/ssh"
)

const (
	maximumSSHKeyCount  = 100
	maximumSSHKeyLength = 16 * 1024
)

type replaceVirtualMachineSSHKeysRequest struct {
	SSHKeys []string `json:"ssh_keys"`
}

// @Summary	Replace virtual machine SSH keys
// @Tags		vms
// @Accept		json
// @Produce	json
// @Param		id		path		string									true	"Virtual machine identifier"
// @Param		body	body		replaceVirtualMachineSSHKeysRequest	true	"Complete SSH key list"
// @Success	200		{object}	virtualMachineResponse
// @Failure	400		{object}	errorResponse
// @Failure	404		{object}	errorResponse
// @Router		/vms/{id}/ssh-keys [put]
func (s *Server) replaceVirtualMachineSSHKeys(c echo.Context) error {
	virtualMachineID := c.Param("id")
	if !validResourceID(virtualMachineID) {
		return badRequest("invalid virtual machine identifier")
	}

	var request replaceVirtualMachineSSHKeysRequest
	if err := c.Bind(&request); err != nil {
		return badRequest("invalid JSON request")
	}
	sshKeys, err := validateSSHKeys(request.SSHKeys)
	if err != nil {
		return badRequest(err.Error())
	}
	if err := s.virtualMachineDriver.ReplaceSSHKeys(
		c.Request().Context(),
		virtualMachineID,
		sshKeys,
	); err != nil {
		return err
	}

	virtualMachine, err := s.loadVirtualMachine(c)
	if err != nil {
		return err
	}
	return s.respondWithVirtualMachine(c, http.StatusOK, virtualMachine)
}

func validateSSHKeys(values []string) ([]string, error) {
	if values == nil {
		return nil, fmt.Errorf("ssh_keys is required")
	}
	if len(values) > maximumSSHKeyCount {
		return nil, fmt.Errorf("ssh_keys cannot contain more than %d keys", maximumSSHKeyCount)
	}

	seen := make(map[string]struct{}, len(values))
	sshKeys := make([]string, 0, len(values))
	for index, value := range values {
		sshKey, identity, err := validateSSHKey(value)
		if err != nil {
			return nil, fmt.Errorf("ssh_keys[%d]: %w", index, err)
		}
		if _, found := seen[identity]; found {
			return nil, fmt.Errorf("ssh_keys[%d]: duplicate key", index)
		}
		seen[identity] = struct{}{}
		sshKeys = append(sshKeys, sshKey)
	}
	return sshKeys, nil
}

func validateSSHKey(value string) (string, string, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return "", "", fmt.Errorf("key is empty")
	}
	if len(value) > maximumSSHKeyLength {
		return "", "", fmt.Errorf("key is too long")
	}
	if strings.ContainsAny(value, "\r\n") {
		return "", "", fmt.Errorf("key must use one line")
	}

	publicKey, _, _, rest, err := ssh.ParseAuthorizedKey([]byte(value))
	if err != nil || len(bytes.TrimSpace(rest)) != 0 {
		return "", "", fmt.Errorf("key is not a valid OpenSSH authorized key")
	}
	return value, string(publicKey.Marshal()), nil
}
