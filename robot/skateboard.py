"""The skateboard: skate/skate.yaml and the catalogue of skate policies it lists.

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
  policy              the name of the policy to run: one of the entries under policies
  speed               m/s the board is asked to roll at (one speed only)
  carve               gentle / medium / hard -> yaw command in rad/s
  default_seconds     a command's length when Gemini gives none
  max_seconds         the longest single command
  queue_max_seconds   the most riding that can be queued (at most MAX_RIDE_S)
  policies            name -> one entry per policy file:
    file              the policy JSON, relative to this folder
    tested            what the policy has done on the real robot
    travel_m_per_s    sim estimate: metres per second of riding
    turn_deg_per_s    gentle / medium / hard -> sim estimate of the board's turn
    coast_m           sim estimate: how far the board rolls after the last command

Only the selected entry is loaded; the robot runs one skate policy, and no tool
lets Gemini change it. The ceilings below are hard: skate.yaml can only go
lower. A value above one, a missing key, a value of the wrong type or a policy
file that is missing or not a policy is a SkateConfigError naming the file and
key. A skate.yaml in the old single-policy layout (a policy file and the
estimates at the top level) is one too.

No skate.yaml means no skate mode: load_skate returns None and the skate
tools say nothing is installed. The robot and the launch file use
load_skate_safe: a bad skate.yaml turns skate mode off (the tools say why)
instead of stopping the robot from starting.

This module has no ROS imports, so the launch file can use it too.
"""

from __future__ import annotations

import json
import math
import os
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
OLD_LAYOUT_KEYS = ("travel_m_per_s", "turn_deg_per_s", "coast_m")  # top-level keys of the old single-policy skate.yaml


class SkateConfigError(ValueError):
    """skate.yaml or the policy it selects is wrong. The message is short enough for Gemini to
    read out: "skate.yaml: " and the key, with no folder path and no Python exception text.
    ``detail`` has the full path and the exception, for the log."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail or message


class _DuplicateKey(Exception):
    def __init__(self, key, line: int):
        super().__init__(key, line)
        self.key, self.line = key, line


class _UniqueKeyLoader(yaml.SafeLoader):
    """yaml.SafeLoader that refuses a key written twice in one mapping (SafeLoader keeps the last)."""


def _unique_mapping(loader: _UniqueKeyLoader, node, deep: bool = False):
    loader.flatten_mapping(node)
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in seen:
                raise _DuplicateKey(key, key_node.start_mark.line + 1)
            seen.add(key)
        except TypeError:  # an unhashable key: construct_mapping refuses it as a YAML error
            pass
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _show(value) -> str:
    """A value for an error message, cut short so the message stays short."""
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def not_installed_message(error: str | None) -> str:
    """What the skate tools say when skate mode is off: not installed, or a config error (load_skate_safe)."""
    if error is None:
        return NOT_INSTALLED
    return f"skate mode is off on this robot because its config has an error ({error}), so Pupper can't skate"


@dataclass
class SkateConfig:
    """How Pupper skates: what it is commanded, and what Gemini is told to expect."""

    name: str  # the catalogue key in skate.yaml's policies
    tested: str  # what the policy has done on the real robot
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
    """The selected policy's skate config, or None when skate.yaml is not there. Raises
    SkateConfigError for a missing or repeated key, a value of the wrong type, numbers the
    policy was not trained on or that are over the limits."""
    spec_path = skate_dir / "skate.yaml"
    if not spec_path.exists():
        return None

    def fail(text: str, cause: BaseException | None = None):
        detail = f"{spec_path}: {text}" + (f" ({type(cause).__name__}: {cause})" if cause is not None else "")
        raise SkateConfigError(f"skate.yaml: {text}", detail)

    try:
        spec = yaml.load(spec_path.read_text(), Loader=_UniqueKeyLoader)
    except _DuplicateKey as e:
        fail(f"the key {e.key} is written twice; keep one", e)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError) as e:
        fail("is not valid YAML", e)
    if not isinstance(spec, dict):
        fail("empty, or not a mapping of the fields in skateboard.py")
    if any(k in spec for k in OLD_LAYOUT_KEYS) or str(spec.get("policy", "")).endswith(".json"):
        fail("uses the old single-policy layout; move the estimates into an entry under policies (see the skate README)")

    def num(table: dict, key: str, where: str = "") -> float:
        if key not in table:
            fail(f"missing {where}{key}")
        value = table[key]
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            fail(f"{where}{key} must be a number, not {_show(value)}")
        try:
            number = float(value)
        except ValueError:
            fail(f"{where}{key} must be a number, not {_show(value)}")
        if not math.isfinite(number):
            fail(f"{where}{key} must be a finite number, not {_show(value)}")
        return number

    def sharp(table: dict, key: str, where: str = "") -> dict[str, float]:
        """A gentle / medium / hard table of numbers."""
        value = table.get(key)
        if not isinstance(value, dict):
            fail(f"{where}{key} must be a mapping of {', '.join(SHARPNESS)}, not {_show(value)}")
        missing = [s for s in SHARPNESS if s not in value]
        if missing:
            fail(f"{where}{key} needs {', '.join(SHARPNESS)}; missing {', '.join(missing)}")
        return {s: num(value, s, f"{where}{key} ") for s in SHARPNESS}

    if "policy" not in spec:
        fail("missing policy")
    name = spec["policy"]
    if not isinstance(name, str):
        fail(f"policy must be the name of an entry under policies, not {_show(name)}")
    catalogue = spec.get("policies")
    if not isinstance(catalogue, dict) or not catalogue:
        fail(f"policies must be a mapping of policy names to entries, not {_show(catalogue)}")
    if name not in catalogue:
        fail(f"policy {_show(name)} is not one of {', '.join(map(str, catalogue))}")
    entry = catalogue[name]
    at = f"policies.{name}."
    if not isinstance(entry, dict):
        fail(f"policies.{name} must be a mapping (file, tested and the sim estimates), not {_show(entry)}")
    for key in ("file", "tested"):
        if key not in entry:
            fail(f"missing {at}{key}")
        if not isinstance(entry[key], str) or not entry[key].strip():
            fail(f"{at}{key} must be text, not {_show(entry[key])}")
    # In the skate folder, checked on the path as written (a symlinked folder is fine).
    rel = os.path.normpath(entry["file"])
    if os.path.isabs(rel) or rel == ".." or rel.startswith(".." + os.sep):
        fail(f"{at}file must be a path inside the skate folder")
    policy_path = skate_dir / rel
    shown = policy_path.name
    if not policy_path.exists():
        fail(f"{at}file: the policy {shown} is not there; copy it into the skate folder (see the skate README)")
    if not policy_path.is_file():
        fail(f"{at}file: {_show(entry['file'])} is not a file")
    try:
        policy = json.loads(policy_path.read_text())
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as e:
        fail(f"{at}file: the policy {shown} is not valid JSON", e)
    if not isinstance(policy, dict):
        fail(f"{at}file: the policy {shown} is not a JSON object, so it is not a policy")
    clip = policy.get("command_clip")
    if not isinstance(clip, dict) or not all(
            isinstance(clip.get(k), list) and len(clip[k]) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in clip[k])
            for k in ("vx", "wz")):
        fail(f"{at}file: the policy {shown} has no command_clip with vx and wz ranges")
    hold = policy.get("heading_hold", {})
    if hold is None:  # JSON null: no heading hold
        hold = {}
    if not isinstance(hold, dict):
        fail(f"{at}file: the policy {shown}'s heading_hold must be a mapping, not {_show(hold)}")
    speed = num(spec, "speed")
    top_speed = max(clip["vx"])
    if not 0.0 < speed <= top_speed + 1e-6:
        fail(f"speed {speed:g} must be above 0 and at most {top_speed:g} m/s, the policy's clip")
    if speed > MAX_SPEED + 1e-6:
        fail(f"speed {speed:g} is over the {MAX_SPEED:g} m/s ceiling")
    carve = sharp(spec, "carve")
    threshold = num(hold, "yaw_threshold", f"the policy {shown}'s heading_hold ") if "yaw_threshold" in hold else 0.0
    if threshold < 0.0:  # a negative threshold would let a negative carve through the bound below
        fail(f"the policy {shown}'s heading_hold yaw_threshold {threshold:g} must be 0 or more")
    lowest = threshold + CARVE_MARGIN
    top_carve = min(abs(clip["wz"][0]), abs(clip["wz"][1]))
    for s, wz in carve.items():
        if not wz > 0.0:  # on its own, whatever the policy file says (the ceiling is checked below)
            fail(f"carve {s} {wz:g} must be above 0 (the tool picks left or right)")
        if not lowest - 1e-6 <= wz <= top_carve + 1e-6:
            fail(f"carve {s} {wz:g} must be from {lowest:g} (clear of the heading hold)"
                 f" to {top_carve:g} rad/s (the policy's clip)")
        if wz > MAX_CARVE + 1e-6:
            fail(f"carve {s} {wz:g} is over the {MAX_CARVE:g} rad/s ceiling")
    if not carve["gentle"] < carve["medium"] < carve["hard"]:
        fail("carve must get sharper from gentle to medium to hard, not "
             + ", ".join(f"{s} {v:g}" for s, v in carve.items()))
    turn = sharp(entry, "turn_deg_per_s", at)
    travel, coast = num(entry, "travel_m_per_s", at), num(entry, "coast_m", at)
    estimates = {"travel_m_per_s": travel, "coast_m": coast, **{f"turn_deg_per_s {s}": v for s, v in turn.items()}}
    for key, v in estimates.items():
        if not v > 0.0:
            fail(f"the estimate {at}{key} {v:g} must be above 0 (measure it in sim)")
    default_s, max_s, queue_s = (num(spec, k) for k in ("default_seconds", "max_seconds", "queue_max_seconds"))
    if queue_s > MAX_RIDE_S + 1e-6:
        fail(f"queue_max_seconds {queue_s:g} is more than {MAX_RIDE_S:g} s")
    if not MIN_RIDE_S <= max_s <= queue_s:
        fail(f"max_seconds {max_s:g} must be from {MIN_RIDE_S:g} s to queue_max_seconds {queue_s:g}")
    if max_s > MAX_COMMAND_S + 1e-6:
        fail(f"max_seconds {max_s:g} is over the {MAX_COMMAND_S:g} s ceiling")
    if not MIN_RIDE_S <= default_s <= max_s:
        fail(f"default_seconds {default_s:g} must be from {MIN_RIDE_S:g} s to max_seconds {max_s:g}")
    return SkateConfig(name=name, tested=entry["tested"], policy_path=policy_path, speed=speed, carve=carve,
                       travel_m_per_s=travel, turn_deg_per_s=turn, coast_m=coast, default_seconds=default_s,
                       max_seconds=max_s, queue_max_seconds=queue_s)


def load_skate_safe(skate_dir: Path = SKATE_DIR) -> tuple[SkateConfig | None, str | None, str | None]:
    """load_skate that never raises, for the robot and the launch file. Returns (config, error,
    detail): (config, None, None); (None, None, None) when skate.yaml is not there; or (None, the
    short error Gemini may read, the full detail for the log) when it is wrong, so skate mode is
    off but the rest of the robot still starts."""
    try:
        return load_skate(skate_dir), None, None
    except SkateConfigError as e:
        return None, str(e), e.detail
    except Exception as e:  # a bug in the checks above must not stop the robot either
        return None, "skate.yaml could not be loaded", f"{skate_dir / 'skate.yaml'}: {type(e).__name__}: {e!r}"


def skate_controller_params(cfg: SkateConfig, base: dict) -> dict:
    """Parameters for the skate controller: the course default controller's, with the
    skate policy (its JSON carries its stance, gains, command clip and heading hold)
    and no joystick game features. The tilt limit and trims stay the course's."""
    params = dict(base)
    params.pop("chord_partner", None)
    params.update(model_path=str(cfg.policy_path), game_buttons=False, reference_tricks=False)
    return params
