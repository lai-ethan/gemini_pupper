"""The skateboard: skate/skate.yaml and the skate policy next to it.

Pupper rides a mini cruiser in a split stance: left feet on the deck, right
feet pushing on the floor. One policy (pupper-mjlab's
Mjlab-SkateSplit-Steer-Pupper-v3) rides and steers. Its speed command is the
board's speed. Its yaw command is a carve: 0 rides straight (the controller's
heading hold keeps the line), and a value above the hold's threshold asks
Pupper to lean so the board turns, at about a tenth of the command.

Pupper cannot mount the board, measure distance or see obstacles. So skate
mode is started by a person who has placed Pupper, every command is timed and
short, and the numbers Gemini reads are estimates from simulation.

skate.yaml fields:
  policy              the policy JSON, in this folder
  speed               m/s the board is asked to roll at (one speed only)
  carve               gentle / medium / hard -> yaw command in rad/s
  travel_m_per_s      sim estimate: metres per second of riding
  turn_deg_per_s      gentle / medium / hard -> sim estimate of the board's turn
  coast_m             sim estimate: how far the board rolls after the last command
  default_seconds     a command's length when Gemini gives none
  max_seconds         the longest single command
  queue_max_seconds   the most riding that can be queued (at most MAX_RIDE_S)

The ceilings below are hard: skate.yaml can only go lower. A value above one,
a missing key or a missing policy clip is a ValueError naming the file and key.

No skate.yaml means no skate mode: load_skate returns None and the skate
tools say nothing is installed.

This module has no ROS imports, so the launch file can use it too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

SKATE_DIR = Path(__file__).resolve().parent.parent / "skate"
SKATE_CONTROLLER = "gemini_skate"  # the ros2_control controller that runs the skate policy
SKATING = "skating"  # the state: the skate policy holds the legs, Pupper is on the board
MIN_RIDE_S = 1.0  # shorter than this barely gets a push in
MAX_RIDE_S = 10.0  # hardware runs are kept to about 10 s; skate.yaml can only go lower
MAX_SPEED = 0.3  # m/s: the only speed checked in sim
MAX_CARVE = 0.5  # rad/s: one sim env lost the board at 0.6
MAX_COMMAND_S = 5.0  # s: the longest single command
CARVE_MARGIN = 0.04  # rad/s a carve must clear the heading hold's threshold by
SHARPNESS = ("gentle", "medium", "hard")

# The only tools Gemini may call while Pupper is on the board (tool_registry.call).
ON_BOARD_TOOLS = {"start_skating", "skate_forward", "skate_turn", "end_skating", "stop", "robot_status"}
ON_BOARD_MESSAGE = (
    "Pupper is on the skateboard. There it can only ride forward (skate_forward), carve left or right"
    " (skate_turn) and stop. It cannot walk, go backward, step sideways, turn on the spot, follow or do"
    " tricks. To get off: a person holds the harness, then call end_skating"
)
# What Gemini hears after a tilt e-stop on the board, instead of robot_api.FELL_MESSAGE:
# Pupper is still on the board, where stand_up is refused, so the way out comes first.
FELL_ON_BOARD_MESSAGE = (
    "Pupper fell on the skateboard: its tilt e-stop tripped and the motors only damp now. It is still on"
    " the board, so it can't stand up there. Ask a person to hold the harness, then call end_skating with"
    " held=true; once they have lifted Pupper off the board, stand_up gets it walking again"
)
# What robot_status says after the gamepad e-stop on the board: the motors are relaxed, Pupper is on it.
STOPPED_ON_BOARD_MESSAGE = (
    "motors relaxed on the skateboard (the e-stop stopped Pupper while it was on the board). It is still on"
    " the board, so it can't stand up there. Ask a person to hold the harness, then call end_skating with"
    " held=true; once they have lifted Pupper off the board, stand_up gets it walking again"
)
NOT_INSTALLED = "no skate policy is installed on this robot (skate/skate.yaml is missing), so Pupper can't skate"


@dataclass
class SkateConfig:
    """How Pupper skates: what it is commanded, and what Gemini is told to expect."""

    policy_path: Path
    speed: float  # m/s
    carve: dict[str, float]  # sharpness -> yaw command, rad/s
    travel_m_per_s: float  # sim estimate
    turn_deg_per_s: dict[str, float]  # sharpness -> sim estimate of the board's turn
    coast_m: float  # sim estimate
    default_seconds: float
    max_seconds: float
    queue_max_seconds: float

    def describe(self) -> str:
        """What Gemini reads about skating (the {skate} in a tool description)."""
        g, m, h = (self.turn_deg_per_s[s] for s in SHARPNESS)
        return (f"Pupper rides at {self.speed:g} m/s, about {self.travel_m_per_s:g} m per second of riding, and the"
                f" board rolls on about {self.coast_m:g} m after it stops pushing (no brake). One command lasts"
                f" {MIN_RIDE_S:g} to {self.max_seconds:g} s (default {self.default_seconds:g} s), and at most"
                f" {self.queue_max_seconds:g} s of riding can be queued. Carving is slow: the board turns about"
                f" {g:g}, {m:g} or {h:g} degrees per second for gentle, medium or hard. These are estimates from"
                f" simulation: Pupper cannot measure distance or see obstacles.")


def load_skate(skate_dir: Path = SKATE_DIR) -> SkateConfig | None:
    """The skate config, or None when skate.yaml is not there. Raises ValueError
    for a missing key, numbers the policy was not trained on or that are over the limits."""
    spec_path = skate_dir / "skate.yaml"
    if not spec_path.exists():
        return None
    spec = yaml.safe_load(spec_path.read_text())
    if not isinstance(spec, dict):
        raise ValueError(f"{spec_path}: empty, or not a mapping of the fields in robot/skateboard.py")

    def num(table: dict, key: str, where: str = "") -> float:
        if key not in table:
            raise ValueError(f"{spec_path}: missing {where}{key}")
        try:
            return float(table[key])
        except (TypeError, ValueError):
            raise ValueError(f"{spec_path}: {where}{key} must be a number, not {table[key]!r}") from None

    if "policy" not in spec:
        raise ValueError(f"{spec_path}: missing policy")
    policy_path = skate_dir / str(spec["policy"])
    if not policy_path.exists():
        raise ValueError(f"{spec_path}: the policy {policy_path.name} is not in {skate_dir}; copy it there"
                         " (see skate/README.md)")
    policy = json.loads(policy_path.read_text())
    clip = policy.get("command_clip")
    if not isinstance(clip, dict) or not all(isinstance(clip.get(k), list) and len(clip[k]) == 2 for k in ("vx", "wz")):
        raise ValueError(f"{spec_path}: the policy {policy_path.name} has no command_clip with vx and wz ranges")
    hold = policy.get("heading_hold") or {}
    speed = num(spec, "speed")
    top_speed = max(clip["vx"])
    if not 0.0 < speed <= top_speed + 1e-6:
        raise ValueError(f"{spec_path}: speed {speed:g} must be above 0 and at most {top_speed:g} m/s, the policy's clip")
    if speed > MAX_SPEED + 1e-6:
        raise ValueError(f"{spec_path}: speed {speed:g} is over the {MAX_SPEED:g} m/s ceiling")
    for table in ("carve", "turn_deg_per_s"):
        missing = [s for s in SHARPNESS if s not in (spec.get(table) or {})]
        if missing:
            raise ValueError(f"{spec_path}: {table} needs {list(SHARPNESS)}; missing {missing}")
    carve = {s: num(spec["carve"], s, "carve ") for s in SHARPNESS}
    lowest = float(hold.get("yaw_threshold", 0.0)) + CARVE_MARGIN
    top_carve = min(abs(clip["wz"][0]), abs(clip["wz"][1]))
    for s, wz in carve.items():
        if not lowest - 1e-6 <= wz <= top_carve + 1e-6:
            raise ValueError(f"{spec_path}: carve {s} {wz:g} must be from {lowest:g} (clear of the heading hold)"
                             f" to {top_carve:g} rad/s (the policy's clip)")
        if wz > MAX_CARVE + 1e-6:
            raise ValueError(f"{spec_path}: carve {s} {wz:g} is over the {MAX_CARVE:g} rad/s ceiling")
    if not carve["gentle"] < carve["medium"] < carve["hard"]:
        raise ValueError(f"{spec_path}: carve must get sharper from gentle to medium to hard, not {carve}")
    turn = {s: num(spec["turn_deg_per_s"], s, "turn_deg_per_s ") for s in SHARPNESS}
    travel, coast = num(spec, "travel_m_per_s"), num(spec, "coast_m")
    estimates = {"travel_m_per_s": travel, "coast_m": coast, **{f"turn_deg_per_s {s}": v for s, v in turn.items()}}
    for key, v in estimates.items():
        if not v > 0.0:
            raise ValueError(f"{spec_path}: the estimate {key} {v:g} must be above 0 (measure it in sim)")
    default_s, max_s, queue_s = (num(spec, k) for k in ("default_seconds", "max_seconds", "queue_max_seconds"))
    if queue_s > MAX_RIDE_S + 1e-6:
        raise ValueError(f"{spec_path}: queue_max_seconds {queue_s:g} is more than {MAX_RIDE_S:g} s")
    if not MIN_RIDE_S <= max_s <= queue_s:
        raise ValueError(f"{spec_path}: max_seconds {max_s:g} must be from {MIN_RIDE_S:g} s to queue_max_seconds {queue_s:g}")
    if max_s > MAX_COMMAND_S + 1e-6:
        raise ValueError(f"{spec_path}: max_seconds {max_s:g} is over the {MAX_COMMAND_S:g} s ceiling")
    if not MIN_RIDE_S <= default_s <= max_s:
        raise ValueError(f"{spec_path}: default_seconds {default_s:g} must be from {MIN_RIDE_S:g} s to max_seconds {max_s:g}")
    return SkateConfig(policy_path=policy_path, speed=speed, carve=carve, travel_m_per_s=travel, turn_deg_per_s=turn,
                       coast_m=coast, default_seconds=default_s, max_seconds=max_s, queue_max_seconds=queue_s)


def skate_controller_params(cfg: SkateConfig, base: dict) -> dict:
    """Parameters for the skate controller: the course default controller's, with the
    skate policy (its JSON carries its stance, gains, command clip and heading hold)
    and no joystick game features. The tilt limit and trims stay the course's."""
    params = dict(base)
    params.pop("chord_partner", None)
    params.update(model_path=str(cfg.policy_path), game_buttons=False, reference_tricks=False)
    return params
