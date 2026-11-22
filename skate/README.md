# Skate mode

Voice control for Pupper on the skateboard (CS 123 Team 9 final project).
Tools: `lab/part8_skate.py`. Numbers: `skate.yaml`. Config loader:
`robot/skateboard.py`.

- `policy.json` is pupper-mjlab's
  `skateboard/policies/split_steer_v6_max0.3_policy.json`. To change policy,
  copy the new file over it and re-measure the estimates in `skate.yaml` with
  pupper-mjlab's `skateboard/tools/timed_ride.py`.
- What a person says: "lie down", place Pupper, "start skating", "go
  forward", "turn left", "stop", hold Pupper, "get off the board".
- Pupper cannot measure distance or see obstacles. Commands are timed
  (1 to 5 s, at most 10 s queued), the speed is fixed at 0.3 m/s, and
  distances and turns in replies are estimates from simulation.
- The 10 s queue cap can refuse a ride a little sooner than 10 s, when an
  earlier job finished faster than its estimate.
- `end_skating` waits behind rides that are already queued. To end at once,
  say "stop" first.
- If the tilt e-stop fires while skating (Pupper tips on the board), Pupper is
  marked fallen. Every walking tool, trick and follow stays refused until a
  person holds Pupper and `end_skating(held=true)` is called, or the gamepad
  e-stop is pressed. `start_skating` is still allowed then.
- Desk test: `python3 lab/test.py skate` (no robot needed).
- A new policy needs a TA's OK before its first run on the robot. The
  hardware checklist is test 6 in pupper-mjlab's
  `skateboard/HARDWARE_TEST.md`.

Nothing here has run on the robot yet. The numbers in `skate.yaml` are sim
estimates from `timed_ride.py` (16 envs, 5 s ride, steer v6), not measurements.
