package api

import "time"

func formatRFC3339(timestamp time.Time) string {
	if timestamp.IsZero() {
		return ""
	}

	return timestamp.Format(time.RFC3339)
}
