package api

import (
	"bytes"
	"fmt"
	"net/http"
	"strings"

	"github.com/labstack/echo/v5"
	"golang.org/x/crypto/ssh"
)

// SSH keys are published to the guest through MMDS, so their size is bounded.
const (
	maximumSSHKeyCount  = 16
	maximumSSHKeyLength = 2 * 1024
)

// replaceVirtualMachineSSHKeysRequest carries the complete SSH key list.
type replaceVirtualMachineSSHKeysRequest struct {
	SSHKeys []string `json:"ssh_keys"`
}

// @Summary	Replace virtual machine SSH keys
// @Description	Store the complete SSH key list. Return 200 after immediate apply or 202 when reconciliation must continue.
// @ID			replaceVirtualMachineSSHKeys
// @Tags		Virtual machines
// @Accept		json
// @Produce	json
// @Param		id		path		string									true	"Virtual machine identifier"
// @Param		request	body		replaceVirtualMachineSSHKeysRequest	true	"Complete SSH key list"
// @Success	200		{object}	virtualMachineResponse
// @Success	202		{object}	virtualMachineResponse
// @Failure	400		{object}	errorResponse
// @Failure	401		{object}	errorResponse
// @Failure	404		{object}	errorResponse
// @Failure	500		{object}	errorResponse
// @Router		/v1/vms/{id}/ssh-keys [put]
func (s *Server) replaceVirtualMachineSSHKeys(c *echo.Context) error {
	identifier, err := virtualMachineID(c)
	if err != nil {
		return err
	}

	var request replaceVirtualMachineSSHKeysRequest
	if err := decodeJSONRequest(c, &request); err != nil {
		return err
	}
	sshKeys, err := validateSSHKeys(request.SSHKeys)
	if err != nil {
		return badRequest(err.Error())
	}
	applied, err := s.virtualMachineManager.ReplaceSSHKeys(
		c.Request().Context(),
		identifier,
		sshKeys,
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

// validateSSHKeys accepts a complete key list and rejects a duplicate. Keys are
// compared by their parsed public key, so 2 spellings of one key still collide.
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

// validateSSHKey parses one authorized key and returns it with its identity.
// Trailing content is rejected, so a line cannot smuggle a second key.
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
