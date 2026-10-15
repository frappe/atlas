package platform

import "context"

// UnitState describes one loaded systemd unit for host telemetry.
type UnitState struct {
	Name        string
	ActiveState string
	SubState    string
}

// UnitStates returns the name and state of each loaded unit that matches
// patterns, such as "metal-vm@*.service". It reads no unit properties, so it
// stays cheap for many units.
func (d *DBus) UnitStates(ctx context.Context, patterns []string) ([]UnitState, error) {
	units, err := d.connection.ListUnitsByPatternsContext(ctx, nil, patterns)
	if err != nil {
		return nil, err
	}

	states := make([]UnitState, 0, len(units))
	for _, unit := range units {
		states = append(states, UnitState{Name: unit.Name, ActiveState: unit.ActiveState, SubState: unit.SubState})
	}
	return states, nil
}

// UnitRestarts returns the automatic restart count of each loaded service that
// matches patterns, by unit name.
func (d *DBus) UnitRestarts(ctx context.Context, patterns []string) (map[string]uint32, error) {
	units, err := d.UnitStates(ctx, patterns)
	if err != nil {
		return nil, err
	}

	restarts := make(map[string]uint32, len(units))
	for _, unit := range units {
		if property, err := d.connection.GetUnitTypePropertyContext(ctx, unit.Name, "Service", "NRestarts"); err == nil {
			restarts[unit.Name] = asUint32(property.Value.Value())
		}
	}
	return restarts, nil
}
