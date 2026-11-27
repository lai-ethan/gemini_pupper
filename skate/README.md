# Skate mode

Voice control for Pupper on the skateboard (CS 123 Team 9 final project).
Tools: `lab/part8_skate.py`. Numbers: `skate.yaml`. Config loader:
`robot/skateboard.py`. Running the demo: [`DEMO.md`](DEMO.md).

- `skate.yaml` is a catalogue. The shared settings are at the top (`speed`,
  the `carve` for gentle, medium and hard, `default_seconds`, `max_seconds`,
  `queue_max_seconds`). `policies:` has one entry per policy file, each with
  its `file`, what it has done on the robot (`tested`) and its sim estimates
  (`travel_m_per_s`, `turn_deg_per_s` for gentle, medium and hard, and
  `coast_m`). `policy:` names the one entry the robot runs; only that entry
  is loaded, and no tool lets Gemini change it.
- `policies/` holds copies of pupper-mjlab's `skateboard/policies/` files
  (steer v6, steer v10 and steer v13, the three catalogue entries; v13 has only
  been run in simulation, never on the robot). To change policy, get Pupper
  off the board, set `policy:` to another name, then run `robot/stop.sh` and
  `robot/start.sh` (`start.sh` alone, with the stack still up, restarts only
  the server and leaves the old policy in the skate controller; see
  `DEMO.md`, "Changing policy"). To add one, copy the
  file into `policies/`, add an entry and measure its estimates with
  pupper-mjlab's `skateboard/tools/timed_ride.py`. A new policy needs a TA's
  OK before its first run on the robot.
- The old layout (`policy: policy.json` with `travel_m_per_s`,
  `turn_deg_per_s` and `coast_m` at the top level) is refused: move those
  three into an entry under `policies:`.
- A bad `skate.yaml` does not stop the robot: skate mode is off, the error is
  logged, and the skate tools tell Gemini the config has an error.
- The robot server also compares the policy file `skate.yaml` selects with
  the `model_path` the launch wrote for `gemini_skate` in its controller
  parameter file (`PARAM_FILE` in `robot/spawn_controllers.py`), which the
  spawner loaded the controller from. If they differ, or the file has no
  skate entry or cannot be read, skate mode is off and Gemini hears that the
  robot software must be stopped and started again. No file at all (a stack
  not started by this repo's launch, or a desk test) leaves skate mode on:
  then the controller was not loaded from that file either. A policy file
  replaced under the same name is not noticed.
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
  person holds Pupper and `end_skating(held=true)` is called. `start_skating`
  is still allowed then. `robot_status` and the skate tools tell Gemini to
  call `end_skating(held=true)` first.
- The gamepad e-stop on the board relaxes every leg at once, as anywhere, but
  Pupper is still on the board, so the same guard stays on: "stand up" is
  refused until a person holds Pupper and `end_skating(held=true)` is called
  ("get off the board"), then lifts it off. `robot_status` says so.
- The board guard covers the web app's debug panel too: its walk, trick,
  stand and lie-down buttons are refused on the board, like the tools. On the
  board, the panel's STOP button (a `/move/velocity` of all zeros) and the
  app's Disconnect button (`/walking/deactivate`) stop the ride the way the
  stop tool does: a zero command goes out and the skate policy keeps
  balancing. Neither relaxes the motors there; only `end_skating`, with
  Pupper held, does. Off the board they do what they always did. The gamepad
  is outside every server guard: on the board, use only its e-stop.
- Only the Disconnect button tells the robot the app is going. A closed or
  reloaded page, a dropped SSH tunnel or Wi-Fi, or a Gemini session that
  closes by itself sends nothing: the rides already queued run to their end
  (at most `queue_max_seconds`, 10 s), no voice stop can reach Pupper, and
  the gamepad e-stop is the stop.
- A known narrow window: a Disconnect that lands in the instant the switch
  into skate mode completes can relax Pupper on the board without the "is
  someone holding it?" check. The legs only relax, they never walk, and the
  person placing Pupper has a hand on the harness at that moment.
- `GET /system/status` has a `skate` block: `installed`, `policy`, `tested`,
  `error` (a bad `skate.yaml`'s message, or the stop-and-start-again error above) and `controller` ("active",
  "inactive", "missing" or "unknown").
- "stop" during `start_skating`'s controller switch can still leave the skate
  policy holding the legs; Pupper then counts as on the board, in skate mode.
  A `start_skating` whose switch fails leaves Pupper in motors idle.
- Desk test: `python3 lab/test.py skate` (no robot needed). Its
  `skate_demo` group scripts the demo's tool calls in `DEMO.md`, once for
  every policy in the catalogue: start, walking refused, rides and carves,
  stop, getting off (stop, then `end_skating` with Pupper held), the gamepad
  e-stop, and a tilt fall. The debug panel's STOP and the Disconnect button
  are in other `skate` groups; what Gemini says, an app lost without the
  Disconnect button, and the real robot are not covered.
- A new policy needs a TA's OK before its first run on the robot. The
  hardware checklist is test 6 in pupper-mjlab's
  `skateboard/HARDWARE_TEST.md`.

Voice skating has not run on the robot yet (each entry's `tested` says what
its policy has done there). The numbers in `skate.yaml` are sim estimates
from `timed_ride.py` (16 envs, 5 s ride), not measurements.
