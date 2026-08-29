package vm

type State string

const (
	StateCreated   State = "created"
	StateRunning   State = "running"
	StatePaused    State = "paused"
	StateStopped   State = "stopped"
	StateFailed    State = "failed"
	StateDestroyed State = "destroyed"
)
