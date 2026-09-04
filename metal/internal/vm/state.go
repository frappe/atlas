package vm

// State is a VM's observed condition. The reconciler drives a VM toward its
// DesiredState.
type State string

const (
	StateUnknown   State = "unknown"
	StateCreated   State = "created"
	StateRunning   State = "running"
	StatePaused    State = "paused"
	StateStopped   State = "stopped"
	StateFailed    State = "failed"
	StateDestroyed State = "destroyed"
)

// IsDesiredState reports whether state is a target that a caller can request.
func IsDesiredState(s State) bool {
	switch s {
	case StateRunning, StateStopped, StatePaused, StateDestroyed:
		return true
	default:
		return false
	}
}
