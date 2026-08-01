# Atlas Garage image

This tree is baked into an ordinary Atlas VM through the `garage` Image Build
recipe. `build.sh` installs garage and leaves service disabled till furhter configuration.
After cloning or provisioning a fleet VM from the promoted image, mark it
`is_garage`, run Configure Garage action.

PS: DO NOT PROVISION MULTIPLE NODES CONCURRENTLY. IT MIGHT CAUSE A RACE CONDITION
