"""Part 8 (final project): skateboard. Finished; read it like basics.py.

Pupper rides a mini cruiser in a split stance: left feet on the deck, right
feet pushing on the floor. One policy rides and steers. Gemini drives it with
the same speed commands the walking tools send (robot.send_speed), but the
rules are different, because Pupper cannot measure distance or see obstacles:

  - A person places Pupper on the board. start_skating only runs after
    Gemini passes placed=true, and only from motors idle or fallen (after a
    fall on the board, the skate policy may take the legs back).
  - Every command is timed and short (skate.yaml: max_seconds), and only so
    much riding can be queued (queue_max_seconds). To keep going, a person
    has to ask again.
  - One speed. A carve is a slow turn while riding: the yaw command asks
    Pupper to lean, and the board turns at about a tenth of it.
  - No backward, sideways, turning on the spot, following or tricks on the
    board: tool_registry.call refuses those tools, and Robot.switch_to
    refuses the controllers.
  - The board has no brake. Stopping means "stop pushing"; it rolls on.
  - Every reply calls its distance and turn an estimate.

The numbers Gemini reads come from skate/skate.yaml ({skate} in a description).
"""

from __future__ import annotations

from part3_follow import prepare_to_move
from robot_api import FALLEN, FELL_MESSAGE, LLM_CMD_RATE, MOTORS_IDLE, WALK_READY_S, Result, Robot, state_name, with_note
from skateboard import FELL_ON_BOARD_MESSAGE, MIN_RIDE_S, NOT_INSTALLED, SHARPNESS, SKATE_CONTROLLER, SKATING, SkateConfig
from tool_registry import Param, tool

PLACEMENT = "left feet on the deck, right feet on the floor, body parallel to the board, a hand on the harness"
NOT_SKATING = ("Pupper is not in skate mode. A person places it on the board first (" + PLACEMENT
               + "); then call start_skating with placed=true")


# ============================================================ what the robot does
def fell(robot: Robot) -> str:
    """What Gemini hears after a fall: a fall on the board is ended with end_skating, not stand_up."""
    return FELL_ON_BOARD_MESSAGE if robot.fell_off_board else FELL_MESSAGE


def estimate(cfg: SkateConfig, seconds: float, direction: str, sharpness: str) -> str:
    """What a ride should do, in words, for the tool's reply. An estimate, and it says so."""
    text = f" Estimate from simulation, not measured: about {cfg.travel_m_per_s * seconds:.1f} m forward"
    if direction != "straight":
        text += f", curving {direction} by about {cfg.turn_deg_per_s[sharpness] * seconds:.0f} degrees"
    return (text + f", then the board rolls on about {cfg.coast_m:g} m. Pupper cannot see obstacles:"
                   " a person must keep the path clear.")


async def start_skating(robot: Robot, placed: bool) -> Result:
    """Hand the legs to the skate policy, once a person has placed Pupper on the board."""
    cfg = robot.skate
    if cfg is None:  # (1) now
        return Result(False, NOT_INSTALLED)
    state = robot.state_after_queue
    if state == SKATING:
        return Result(True, "already in skate mode, on the board")
    if state not in (MOTORS_IDLE, FALLEN):
        return Result(False, f"can't start skating from {state_name(state)}: call lie_down first so the motors relax,"
                             f" have a person place Pupper on the board ({PLACEMENT}), then call start_skating")
    if not placed:
        return Result(False, f"not started: Pupper cannot get on the board by itself. Ask a person to place it"
                             f" ({PLACEMENT}), and call start_skating with placed=true once they say it is done")
    note = await prepare_to_move(robot)

    async def job() -> Result:  # (2) later
        res = await robot.switch_to(SKATE_CONTROLLER)
        if not res.ok:
            return res
        robot.send_speed(0, 0, 0)
        await robot.sleep(WALK_READY_S)  # the controller eases into the stance, then fades the policy in
        if robot.fallen:
            return Result(False, fell(robot))
        return Result(True, "in skate mode: balancing on the board, not rolling yet")

    res = await robot.queue("start skating", job, WALK_READY_S, then=SKATING)  # (3)
    return with_note(note, res)


async def ride(robot: Robot, seconds: float, direction: str = "straight", sharpness: str = "medium") -> Result:
    """Ride forward for ``seconds``, straight or carving left or right, then stop pushing."""
    cfg = robot.skate
    if cfg is None:  # (1) now
        return Result(False, NOT_INSTALLED)
    if robot.fallen:
        return Result(False, fell(robot))
    if robot.state_after_queue != SKATING:
        return Result(False, NOT_SKATING)
    if direction not in ("straight", "left", "right") or sharpness not in SHARPNESS:
        return Result(False, f"direction must be straight, left or right and sharpness one of {list(SHARPNESS)}")
    if not MIN_RIDE_S - 1e-6 <= seconds <= cfg.max_seconds + 1e-6:
        return Result(False, f"{seconds:g} s is outside what one skate command may last: {MIN_RIDE_S:g} s to"
                             f" {cfg.max_seconds:g} s. Pupper cannot see where it is going, so rides are short")
    pending = max(0.0, robot.busy_until - robot.now())
    if pending + seconds > cfg.queue_max_seconds + 1e-6:
        return Result(False, f"not queued: {pending:.1f} s is already queued, and with {seconds:g} s more it would pass"
                             f" the {cfg.queue_max_seconds:g} s limit. Wait until Pupper has finished, then ask again")
    wz = 0.0 if direction == "straight" else cfg.carve[sharpness] * (1.0 if direction == "left" else -1.0)
    what = "forward" if direction == "straight" else f"carving {direction} ({sharpness})"

    async def job() -> Result:  # (2) later
        if robot.fallen:
            return Result(False, fell(robot))
        if robot.state_now != SKATING:
            return Result(False, f"did not ride: Pupper is in {state_name(robot.state_now)}, not in skate mode")
        end = robot.now() + seconds
        while robot.now() < end - 1e-6:
            if robot.fallen:
                robot.send_speed(0, 0, 0)
                return Result(False, fell(robot))
            robot.send_speed(cfg.speed, 0.0, wz * 57.2958)  # send_speed takes deg/s
            await robot.sleep(1.0 / LLM_CMD_RATE)
        robot.send_speed(0, 0, 0)
        return Result(True, f"rode {what} for {seconds:g} s, then stopped pushing")

    res = await robot.queue(f"skate {what} {seconds:.3g}s", job, seconds, then=SKATING)  # (3)
    return Result(True, res.message + estimate(cfg, seconds, direction, sharpness)) if res.ok else res


async def end_skating(robot: Robot, held: bool) -> Result:
    """Leave skate mode: stop pushing and relax the motors. A person must be holding Pupper."""
    # On the board now or once the queue has run (an end already queued still leaves Pupper on the
    # board until it runs), or fallen on it: there is something to end.
    if SKATING not in (robot.state_now, robot.state_after_queue) and not robot.fell_off_board:
        return Result(False, "Pupper is not in skate mode, so there is nothing to end")
    if not held:
        return Result(False, "not ended: leaving skate mode relaxes the motors, and Pupper would drop onto the"
                             " board. Ask a person to hold the harness, and call end_skating with held=true once they do")

    async def job() -> Result:
        robot.send_speed(0, 0, 0)
        return await robot.switch_to(None)

    res = await robot.queue("end skating", job, 1.0, then=MOTORS_IDLE)
    return Result(True, res.message + " The motors will relax: lift Pupper off the board. On the floor,"
                                      " stand_up gets it walking again.") if res.ok else res


# ============================================================ what Gemini reads
@tool(
    "start_skating",
    "Start skate mode. Pupper cannot climb onto the skateboard: a person places it first (" + PLACEMENT + ")."
    " Pupper must be relaxed (call lie_down first if it is standing). Ask the person to place it, wait until they"
    " say it is done, and only then call this with placed=true. It takes about 4 s; then Pupper balances on the"
    " board without rolling. {skate}",
    placed=Param("boolean", "true only if a person has just said Pupper is placed on the board. Never guess."),
)
async def start_skating_tool(robot: Robot, placed: bool) -> Result:
    return await start_skating(robot, placed)


@tool(
    "skate_forward",
    "Ride the skateboard straight ahead for a few seconds, then stop pushing. Only in skate mode. Use it for"
    ' "go forward", "ride", "skate". Pupper cannot go a set distance: you pick the seconds. {skate} Before you'
    " call it, the path must be clear; if you are not sure, ask.",
    seconds=Param("number", "How long to ride, in seconds. Leave it out for the default.", required=False),
)
async def skate_forward_tool(robot: Robot, seconds: float | None = None) -> Result:
    cfg = robot.skate
    return await ride(robot, cfg.default_seconds if cfg and seconds is None else (seconds or 0.0))


@tool(
    "skate_turn",
    "Carve left or right while riding forward for a few seconds, then stop pushing. Only in skate mode. Use it"
    ' for "turn left", "go right", "steer". It is a wide, slow curve, not a turn on the spot: say so, and give the'
    " estimate from the reply instead of promising a corner. {skate}",
    direction=Param("string", "Which way to carve.", enum=["left", "right"]),
    sharpness=Param("string", "How hard to lean. Default medium.", enum=list(SHARPNESS), required=False),
    seconds=Param("number", "How long to carve, in seconds. Leave it out for the default.", required=False),
)
async def skate_turn_tool(robot: Robot, direction: str, sharpness: str = "medium", seconds: float | None = None) -> Result:
    cfg = robot.skate
    return await ride(robot, cfg.default_seconds if cfg and seconds is None else (seconds or 0.0), direction, sharpness)


@tool(
    "end_skating",
    "Leave skate mode: Pupper stops pushing and its motors relax, so it drops unless someone holds it. Ask a"
    " person to hold the harness, wait until they say they have it, then call this with held=true. Afterwards the"
    " person lifts Pupper off the board, and stand_up gets it walking again. To only stop riding, call stop instead.",
    held=Param("boolean", "true only if a person has just said they are holding Pupper. Never guess."),
)
async def end_skating_tool(robot: Robot, held: bool) -> Result:
    return await end_skating(robot, held)
