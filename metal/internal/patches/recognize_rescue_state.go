package patches

func recognizeRescueState(string) error {
	// The marker prevents upgrade rollback to a binary that rejects rescue fields. Existing records need no conversion.
	return nil
}
