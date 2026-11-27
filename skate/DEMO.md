# Skate demo: operator runbook

How to run the voice skateboard demo: Pupper on the board, a person talking,
Gemini calling the skate tools. Read it through once before the day.

Read this first:

- **The skate tools in this repo have never run on the robot.** Everything
  below that says what Pupper will do comes from the code and the desk tests.
  `python3 lab/test.py skate_demo` runs the demo's tool calls on a fake
  robot, for every policy in `skate.yaml`: start (refused until placed),
  walking refused, a forward ride, gentle and hard carves, "stop", the
  gamepad e-stop, a tilt fall and getting off. It does not cover what Gemini
  says or asks, the debug panel's STOP, the app disconnecting (other desk
  tests in the `skate` group cover those two), or anything on the real
  robot.
- **Every distance and turn is a sim estimate**, from pupper-mjlab's
  `skateboard/tools/timed_ride.py` (16 boards, 5 s rides). None has been
  measured on the robot.
- What has happened on the robot so far: on 2026-11-26, driven by gamepad
  (not through Gemini Pupper), steer v6 and steer v10 each rolled the board
  forward on carpet for a whole clip. Nothing was measured.
- The board drifts sideways over long rides in sim (tens of cm in 25 s).
  The 2026-11-26 clips showed it drift too; not measured. Keep rides short.
- The gamepad e-stop (right-stick press) outranks everything. One person
  holds it, one keeps a hand on the harness, the whole time.

## Before the day

- [ ] **The student's explicit OK, right then, before `robot/start.sh`**,
      with them at the robot and a hand near the e-stop (AGENTS.md, safety
      rule 1). Ask again after any policy change, before the restart, and
      before the first ride of the new policy.
- [ ] **A TA's OK for each policy file you may run** (`policies/` in this
      folder, listed in `skate.yaml`). AGENTS.md: a new policy needs a TA's
      OK before its first run on the robot. The repo does not say how the OK
      is given or recorded: confirm that with the TA, and write down who
      gave it, when, and for which file.
- [ ] **`policy:` in `skate.yaml`** names the policy for the demo
      (`steer_v6` now). See "Changing policy". Steer v6 is the one to run
      first; steer v10 is provisional.
- [ ] **`python3 lab/test.py skate` passes**, on the robot's copy of the
      repo, after the last `git pull`.
- [ ] pupper-mjlab's `skateboard/HARDWARE_TEST.md` says voice control (its
      test 6) comes after its test 5 (straight ride and steer by gamepad) has
      passed. As of 2026-11-26, test 5 is not recorded as passed. Decide with
      the TA whether the demo goes ahead without it.
- [ ] **The board.** Trucks loosened, so that leaning turns the board: how
      loose has not been recorded, so check it by hand on the day. Find the
      nose: Pupper faces the end with the castle picture, and the board rolls
      that end first. The sheet says no doc tells that end on the real board
      beyond that name, so check which end has the castle picture before you
      place Pupper.
- [ ] The surface. Carpet is the only surface either policy has rolled the
      board on. Test 6 of the sheet asks for 4 m of clear floor ahead.
- [ ] The Gemini key is set up on the robot (`robot/setup.sh` saves it; see
      the README). Never in this repo.
- [ ] A gamepad connected to Pupper, for its e-stop.

## Start

1. Pupper lies on the floor, away from the board.
2. Only with the student's OK, given right then (see "Before the day").
   Start the robot the normal way (README, "Running"): on the Pupper,
   `robot/start.sh` (or `robot/start.sh --kiosk` for the app on Pupper's own
   screen). From a laptop, open the SSH tunnel the README gives, open
   http://localhost:8000, open **Settings**, paste the key and press
   **WAKE PUPPER**.
3. Open http://localhost:8000/system/status in a browser tab (a read-only
   GET). Its `skate` block should read, for steer v6:

   ```
   "skate": {"installed": true, "policy": "steer_v6", "tested": "robot 2026-11-26: ...",
             "error": null, "controller": "inactive"}
   ```

   - `installed: true` and the `policy` you chose in `skate.yaml`.
   - `controller: "inactive"`: the skate controller is loaded and not holding
     the legs. It must not be `"active"` before "start skating" (sheet,
     test 6); if it is, press the e-stop and run `robot/stop.sh`.
   - `"missing"`: there is no skate config, or the controller manager does not
     list `gemini_skate`. Check the spawner's lines in
     `/tmp/gemini-pupper/robot.log`.
   - `"unknown"`: the server or the controller manager is not ready yet, or
     asking it failed. Wait and reload the tab.
   - `error` set (and `installed: false`): `skate.yaml` has a mistake, and
     skate mode is off (the rest of the robot still works). Read the error:
     it names the key. Fix `skate.yaml`, then `robot/stop.sh` and
     `robot/start.sh`, and check this block again.

## Placement

Pupper cannot get on the board. A person places it, Pupper relaxed (lying
down). This is what Gemini reads (`PLACEMENT` in `lab/part8_skate.py`):

> Pupper beside the board on the board's right, body parallel to it and
> facing the end with the castle picture, left feet on the deck's middle
> line, one ahead of the middle and one behind, right feet on the floor, a
> hand on the harness

The body is beside the board, not on it: its centre is over the floor,
outside the deck's edge, and it does not rest on the deck. "Left" is Pupper's
own left. The numbers below are from pupper-mjlab's
`skateboard/HARDWARE_TEST.md` ("Placement (read this first)"). They are sim
values (foot numbers are the centre of each foot ball); none of them has been
measured on the robot.

| What | Sim value |
| --- | --- |
| Board centre line from the body centre, sideways | 14.6 cm, board to Pupper's left |
| Body centre from the board's middle, along the board | 1 cm ahead of it |
| Body centre past the deck's near (right) edge | about 6.7 cm |
| Left feet along the board | front left 7.6 cm ahead of the middle, back left 7.4 cm behind it |
| Left feet from the deck centre line | within about 1 cm of it |
| Left feet to the deck edges | 6.5 to 8.5 cm from either edge |
| Right feet from the board centre line | about 22 to 24 cm to the right |
| Right feet along the board | about 6.5 to 7.5 cm ahead of and behind the middle |
| Body centre height above the floor | 15.4 cm |
| Body tilt | 4.8 degrees (0.083 rad), right side lower |

The sheet gives no number for the gap between the torso and the deck, or
for how far the torso's left side overhangs the deck.

How far off it may start (the range the policies trained on): place the body
within about 1 cm of its spot, the board's middle within about 3 cm of the
body's along the deck, and Pupper square to the board. Tape marks on the
deck centre line, about 7.5 cm ahead of and behind the board's middle, give
the two left-foot spots.

Wrong (as in the 2026-11-24 videos): the body on top of the deck, the left
legs past the far edge of the deck, the body resting on the deck. Each gives
the policy a stance it never trained on.

## Script

One person talks; one holds the harness; one holds the gamepad e-stop. The
estimates Gemini reads depend on the policy loaded:

| | steer_v6 | steer_v10 |
| --- | --- | --- |
| "go forward" (3 s, the default) | about 0.9 m forward | about 0.9 m forward |
| "turn left gently" (3 s) | about 3 degrees | about 2 degrees |
| the board rolls on after the last push | about 0.1 m | about 0.2 m |

These are `travel_m_per_s × 3`, `turn_deg_per_s.gentle × 3` and `coast_m` from
`skate.yaml`, rounded the way the tool replies round them. All sim estimates.

1. **"Lie down."** Pupper lies down; the motors relax.
2. **Place Pupper** (see "Placement"). Hand on the harness.
3. **"Start skating."** Gemini should ask whether Pupper is placed, and only
   call `start_skating` once you say it is. If it calls it with
   `placed=false`, the tool refuses and gives the placement text. The reply
   is "start skating queued"; Pupper eases into the stance (about 4 s) and
   balances without rolling. Asked for status, Gemini hears "skating (policy
   steer_v6)".
4. **"Go forward."** Gemini should ask whether the path is clear, unless you
   just said so. Pupper pushes for 3 s at 0.3 m/s, then stops pushing. The
   reply ends with "Estimate from simulation, not measured: about 0.9 m
   forward, then the board rolls on about 0.1 m. Pupper cannot see
   obstacles: a person must keep the path clear." (steer v6; for steer v10
   the roll is 0.2 m). Gemini should say "about".
5. **"Turn left gently."** A gentle carve: Pupper rides forward at 0.3 m/s
   and leans left (a yaw command of 0.15 rad/s). If Gemini leaves the time
   out it is 3 s, and the reply says "about 0.9 m forward, curving left by
   about 3 degrees" (steer v6) or "... about 2 degrees" (steer v10). It is a wide, slow curve, not
   a corner: expect the heading to move only a few degrees. If the board
   rides dead straight, write it down; do not raise the carve.
6. **"Stop."** Gemini calls `stop` first. Pushing stops; the board has no
   brake and rolls on a little. Pupper stays in skate mode, balancing.
7. **"Get off the board."** Gemini should ask whether someone is holding
   Pupper, and only call `end_skating` once you say so. Then the motors
   relax: hold Pupper, lift it off the board onto the floor. On the floor,
   "stand up" gets it walking again.

What Gemini refuses on the board, and says why: walking, going backward,
stepping sideways, turning on the spot, following, tricks, standing up and
lying down. "Walk forward" on the board is a good thing to show.

## If something goes wrong

- **Anyone says "stop".** Gemini calls `stop` before anything else. The ride
  ends, a zero command goes out, Pupper keeps balancing in skate mode, and the
  board rolls on a little. A "get off the board" said while rides are still
  queued waits for them: say "stop" first to get off at once.
- **The debug panel's STOP button.** On the board it does what "stop" does:
  it ends the ride, and the skate policy keeps balancing. It does not relax
  the motors. The panel's other buttons (walk, trick, stand, lie down) are
  refused on the board.
- **The gamepad e-stop** (right-stick press). The motors relax at once,
  whatever Gemini is doing. After it is pressed, a ride command or two may
  still go out for up to about a tenth of a second before the motors relax
  (measured on the desk test's clock, not on the robot). Pupper is still on the board, so the board guard
  stays on: "stand up" is refused, and `robot_status` says the motors are
  relaxed on the board. To go on: place Pupper again if it moved, and say
  "start skating" (Gemini asks whether it is placed). To get off instead:
  hold Pupper, say "get off the board", lift it off, then "stand up".
- **Pupper tips on the board** (the skate policy's tilt e-stop fires). The
  motors only damp. Walking, follow and tricks are refused, and so is "get
  off the board" until you say Pupper is held. Hold the harness, say "get off
  the board": the motors relax. Lift Pupper off the board onto the floor,
  upright, then say "stand up". (Placing it again and saying "start skating"
  is also allowed.)
- **The app disconnects** (or the page is closed). The ride stops, and the
  skate policy keeps balancing: the motors are not relaxed, unlike a
  disconnect off the board, which lies Pupper down. Keep a hand on the
  harness. Reconnect (**WAKE PUPPER**), then go on, or say "get off the
  board". If the app will not come back, the e-stop relaxes the motors.
  The board guard then stays on until `end_skating`, which needs the app.
  The only other way out is `robot/stop.sh`, then `robot/start.sh`, with a
  person holding Pupper and the student's OK (see "Before the day"); lift
  Pupper off the board before starting again.
- **A ride drifts** to one side or the board wanders. Say "stop" first. Then
  hold Pupper and straighten the board, check the placement, and go on with
  shorter rides. E-stop if the board heads for anything, a left foot nears
  the deck edge or a right foot lands on the deck.
- **The board does not roll.** The repo has no fix for this; these are
  checks, not known causes: the placement (left feet on the deck centre
  line, body beside the board), Pupper facing the castle end, the trucks,
  and the floor (the policies have only rolled the board on carpet). Do not
  raise the speed, the carve or the gains: the limits in
  `robot/skateboard.py` are ceilings, and `skate.yaml` can only go lower.

Stop testing and let the motors cool if Pupper is hot or a motor faults.

## Changing policy

1. In the robot's copy of the repo, set `policy:` in `skate/skate.yaml` to
   another name under `policies:`.
2. With the student's OK, given right then: `robot/stop.sh`, then
   `robot/start.sh` (the robot stack reads `skate.yaml` when it starts).
3. Check `/system/status`: the `skate` block names the new policy, with
   `controller: "inactive"` and no `error`.

No tool lets Gemini change the policy. Each new policy file needs a TA's OK
before its first run on the robot.

| | steer_v6 | steer_v10 |
| --- | --- | --- |
| File | `policies/split_steer_v6_max0.3_policy.json` | `policies/split_steer_v10_max0.3_policy.json` |
| On the robot (`tested`) | 2026-11-26: rolled forward on carpet by gamepad; not measured | 2026-11-26: rolled forward on carpet by gamepad; not measured |
| Through Gemini Pupper | never | never |
| Sim: travel | 0.29 m per second of riding | 0.30 m per second of riding |
| Sim: turn, gentle / medium / hard | 1.1 / 1.4 / 2.2 degrees per second | 0.8 / 0.9 / 1.1 degrees per second |
| Sim: roll after the last push | 0.1 m | 0.2 m |
| Note | the one to run first | provisional: missed its sim bar at x10 wheel drag |

Not tested on either: steering on the robot, any distance or turn, a hard
floor, the voice tools.

## Limits

Enforced by the code (`robot/skateboard.py`, `skate.yaml`, `lab/part8_skate.py`),
not only by the prompt:

- One speed: 0.3 m/s. No faster, no slower.
- One command lasts 1 to 5 s (3 s if Gemini gives none). At most 10 s of
  riding can be queued; to keep going, a person has to ask again.
- Carves: gentle 0.15, medium 0.3, hard 0.5 rad/s of yaw command. The board
  turns far less than that (see the estimates above).
- Forward only: no reverse, no sideways, no turning on the spot.
- No steering or riding by distance: Pupper cannot measure distance or see
  obstacles, so Gemini picks seconds, and a person keeps the path clear.
- Hands off the gamepad sticks and its controller-switch buttons while
  Pupper is on the board: the gamepad outranks Gemini, is outside every
  server guard, and can carve harder than these limits. Use it for its
  e-stop only.
