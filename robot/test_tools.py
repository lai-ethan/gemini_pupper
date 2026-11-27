#!/usr/bin/env python3
"""Desk tests for Gemini Pupper: no robot, no ROS, no Gemini needed.

Run them after every change to a file in lab/ (lab/test.py runs this file):

    python3 lab/test.py            # everything
    python3 lab/test.py todo3      # one TODO (todo1 ... todo11)
    python3 lab/test.py part2      # one part of the lab (part1 ... part7; part6, the spider, is the notebook)
    python3 lab/test.py robot      # the provided robot (should always pass)
    python3 lab/test.py skate      # the skateboard (skate_demo: the demo in skate/DEMO.md, every policy)
    python3 lab/test.py optional_jump   # the optional jump

A fake robot stands in for ROS. It keeps track of which controllers are on,
records every message the robot sends, and plays trick clips instantly. Time
is fake too, so a 10 s walk takes no real time.

The tests check what each tool does and what Gemini is told, not how you wrote
the code. When a test fails, it prints what was asked and what happened.

Passing these tests does not mean the robot is ready. That is what the real
Pupper, the e-stop and a TA are for.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import re
import sys
import tempfile
import types
import yaml
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


# ----------------------------------------------------------------------------- fake ROS
class _Msg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Vec:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = x, y, z


class _Quat:
    def __init__(self, x=0.0, y=0.0, z=0.0, w=1.0):
        self.x, self.y, self.z, self.w = x, y, z, w


class _Twist:
    def __init__(self):
        self.linear, self.angular = _Vec(), _Vec()


class _DataMsg:
    def __init__(self, data=None):
        self.data = data


class _Srv:
    Request = _Msg


class _SwitchController:
    class Request(_Msg):
        STRICT = 2
        BEST_EFFORT = 1

        def __init__(self):
            super().__init__(activate_controllers=[], deactivate_controllers=[], strictness=1)


class FakeRobot:
    """The controller manager, the topics and the person follower, all in one."""

    def __init__(self, clock: "VirtualClock"):
        self.clock = clock
        self.controllers: dict[str, str] = {}
        self.published: list[tuple[float, str, object]] = []  # (time, topic, msg)
        self.switches: list[tuple[float, list[str], list[str]]] = []  # (time, deactivated, activated)
        self.subs: dict[str, list] = {}
        self.follower_on = False
        self.follow_calls: list[str] = []
        self.start_clips: dict[str, list[str]] = {}  # controller -> start_clip values set, in order
        self.switch_delay = 0.0  # s a switch takes to answer (the switch itself happens at once)
        self.refuse: set[str] = set()  # controllers the controller manager refuses to activate
        self.unready: set[str] = set()  # services not advertised yet (service_is_ready is False)

    def active(self) -> set[str]:
        return {n for n, s in self.controllers.items() if s == "active"}

    def twists(self) -> list[tuple[float, _Twist]]:
        return [(t, m) for t, topic, m in self.published if topic == "/llm_cmd_vel"]

    def clips(self, controller: str) -> list[str]:
        return [m.data for _, topic, m in self.published if topic == f"/{controller}/clip"]

    def activations(self, controller: str) -> list[float]:
        return [t for t, _, on in self.switches if controller in on]

    def deactivations(self, controller: str) -> list[float]:
        return [t for t, off, _ in self.switches if controller in off]

    def send(self, topic: str, msg) -> None:
        """Deliver a message to the robot's subscribers."""
        for cb in self.subs.get(topic, []):
            cb(msg)

    def handle(self, service: str, req):
        if service.endswith("list_controllers"):
            return _Msg(controller=[_Msg(name=n, state=s) for n, s in self.controllers.items()])
        if service.endswith("switch_controller"):
            if self.refuse & set(req.activate_controllers):
                return _Msg(ok=False)
            for c in req.deactivate_controllers:
                self.controllers[c] = "inactive"
            for c in req.activate_controllers:
                self.controllers[c] = "active"
            self.switches.append((self.clock.now, list(req.deactivate_controllers), list(req.activate_controllers)))
            return _Msg(ok=True)
        if service.endswith("/set_parameters"):
            controller = service.split("/")[1]
            for prm in req.parameters:
                if prm.name == "start_clip":
                    self.start_clips.setdefault(controller, []).append(prm.value.string_value)
            return _Msg(results=[_Msg(successful=True)])
        if service == "activate_person_following":
            self.follower_on = True
            self.follow_calls.append("on")
            return _Msg(success=True)
        if service == "deactivate_person_following":
            self.follower_on = False
            self.follow_calls.append("off")
            return _Msg(success=True)
        raise AssertionError(f"unexpected service {service}")

    def publish(self, topic: str, msg) -> None:
        self.published.append((self.clock.now, topic, msg))
        if topic.endswith("/clip"):
            controller = topic.split("/")[1]
            if self.controllers.get(controller) == "active":  # a stopped controller hears nothing
                self.send(f"/{controller}/clip_status", _DataMsg(f"{msg.data} holding frame 0 queued 0"))


ROBOT: FakeRobot | None = None


class _Future:
    def __init__(self, result, done_at=0.0):
        self._result = result
        self._done_at = done_at

    def done(self):
        return ROBOT.clock.now >= self._done_at

    def result(self):
        return self._result


class _Client:
    def __init__(self, name):
        self.name = name

    def wait_for_service(self, timeout_sec=None):
        return True

    def service_is_ready(self):
        return self.name not in ROBOT.unready

    def call_async(self, req):
        delay = ROBOT.switch_delay if self.name.endswith("switch_controller") else 0.0
        return _Future(ROBOT.handle(self.name, req), ROBOT.clock.now + delay)


class _Pub:
    def __init__(self, topic):
        self.topic = topic

    def publish(self, msg):
        ROBOT.publish(self.topic, msg)


class _Node:
    def __init__(self, name):
        pass

    def create_client(self, srv_type, name):
        return _Client(name)

    def create_publisher(self, msg_type, topic, qos):
        return _Pub(topic)

    def create_subscription(self, msg_type, topic, cb, qos):
        ROBOT.subs.setdefault(topic, []).append(cb)


class _Executor:
    def add_node(self, node):
        pass

    def spin(self):
        pass


def _install_fake_ros() -> None:
    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m

    mod("rclpy", ok=lambda: True, init=lambda: None)
    mod("rclpy.node", Node=_Node)
    mod("rclpy.executors", SingleThreadedExecutor=_Executor)
    mod("controller_manager_msgs")
    mod("controller_manager_msgs.srv", ListControllers=_Srv, SwitchController=_SwitchController)
    mod("geometry_msgs")
    mod("geometry_msgs.msg", Twist=_Twist)
    mod("rcl_interfaces")
    mod("rcl_interfaces.msg", Log=_Msg, Parameter=_Msg, ParameterValue=_Msg,
        ParameterType=types.SimpleNamespace(PARAMETER_STRING=4))
    mod("rcl_interfaces.srv", SetParameters=_Srv)
    mod("sensor_msgs")
    mod("sensor_msgs.msg", CompressedImage=_Msg, Imu=_Msg, JointState=_Msg)
    mod("std_msgs")
    mod("std_msgs.msg", Empty=_DataMsg, Float32MultiArray=_DataMsg, Float64MultiArray=_DataMsg, String=_DataMsg)
    mod("std_srvs")
    mod("std_srvs.srv", Trigger=_Srv)


_install_fake_ros()
import robot_api  # noqa: E402
import tool_registry as tools  # noqa: E402  (call and declarations: what the web app uses)
behaviors = tools.load_lab()  # noqa: E402  (everything in lab/: lab.walk, lab.check_move, ...)
check_move = behaviors.check_move
from robot_api import LIMITS, LLM_CMD_RATE, LOCOMOTION, LOCOMOTION_CONTROLLER, WALK_READY_S, Robot  # noqa: E402
from tricks import Trick, WalkConfig  # noqa: E402
import skateboard  # noqa: E402
from skateboard import SKATE_CONTROLLER, SKATING, SkateConfig, load_skate, skate_controller_params  # noqa: E402

_real_sleep = asyncio.sleep


class VirtualClock:
    """The robot's time.monotonic() and asyncio.sleep(), with no real waiting."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    async def sleep(self, seconds, result=None):
        self.now += max(0.0, float(seconds))
        await _real_sleep(0)
        return result


class _AsyncioShim(types.ModuleType):
    def __init__(self, clock):
        super().__init__("asyncio")
        self._clock = clock

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, seconds, result=None):
        return await self._clock.sleep(seconds, result)


# ----------------------------------------------------------------------------- fixtures
def _clip(name, start, end, loop=False, next=None):
    return {"name": name, "start": start, "end": end, "loop": loop, "next": next}


_REFERENCE = Path(tempfile.gettempdir()) / "gemini_pupper_test_hop.json"


def fixture_tricks() -> dict[str, Trick]:
    """Made-up tricks: a policy that ends standing, one that ends lying down, a clip
    trick that holds a pose, and a reference trick. Clip lengths are in frames at 50 fps."""
    _REFERENCE.write_text(json.dumps({"playback_s": 1.0, "active_s": 0.8, "cross_fade_s": 0.1,
                                      "table": [[0.0] * 12] * 3}))
    return {
        "bow": Trick(name="bow", description="Bows.", kind="policy", path=Path("bow.json"), duration=4.0),
        "roll_over": Trick(name="roll_over", description="Rolls over and stays lying down.", kind="policy",
                           path=Path("roll_over.json"), duration=6.0, start_pose="lying", end_pose="lying"),
        "hold_pose": Trick(
            name="hold_pose", description="Gets into a pose and holds it.", kind="policy",
            path=Path("hold_pose.json"), duration=12.0, end_pose="sitting",
            clips=[
                _clip("enter", 0, 150, next="idle"),
                _clip("idle", 149, 200, loop=True),
                _clip("wave_left", 199, 300, next="idle"),
                _clip("wave_right", 299, 400, next="idle"),
                _clip("leave", 399, 550),
            ],
            actions={"wave_left": "Waves the left paw.", "wave_right": "Waves the right paw.",
                     "leave": "Stands back up."},
            exit_clip="leave",
        ),
        "hop": Trick(name="hop", description="Hops in place.", kind="reference", path=_REFERENCE, duration=1.0),
    }


class Rig:
    """A fresh Robot wired to a fresh fake robot, standing in locomotion mode."""

    def __init__(self, tricks=None, skate=None, skate_error=None, skate_detail=None):
        """skate: the SkateConfig the robot loads (None: not installed); skate_error:
        instead, the message of a skate.yaml that failed to load (skate mode off), and
        skate_detail the full detail load_skate_safe gives for the log."""
        global ROBOT
        self.clock = VirtualClock()
        ROBOT = self.robot = FakeRobot(self.clock)
        made = tricks or fixture_tricks
        robot_api.time = types.SimpleNamespace(monotonic=self.clock.monotonic)
        robot_api.asyncio = _AsyncioShim(self.clock)
        robot_api.load_tricks = made
        robot_api.load_skate_safe = lambda: (skate, skate_error, skate_detail)
        names = (set(robot_api.LEG_CONTROLLERS) | {t.controller for t in made().values()}
                 | {t.walk_controller for t in made().values() if t.walks})
        if skate is not None:
            names.add(SKATE_CONTROLLER)
        self.robot.controllers = {n: "inactive" for n in names}
        self.robot.controllers[LOCOMOTION_CONTROLLER] = "active"
        self.records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = self.records.append
        for name in ("robot_api", "tools", "behaviors"):
            lg = logging.getLogger(name)
            lg.handlers[:] = [handler]
            lg.setLevel(logging.INFO)
            lg.propagate = False
        self.ts = Robot()

    async def settle(self, limit: int = 200_000) -> bool:
        """Let the queue run until it is empty. False if it never empties."""
        for _ in range(limit):
            await _real_sleep(0)
            if self.ts.commands.empty() and self.ts.current is None:
                await _real_sleep(0)
                if self.ts.commands.empty() and self.ts.current is None:
                    return True
        return False

    async def call(self, name: str, /, **args):
        """Call a tool the way Gemini does, and let the queue run it."""
        res = await tools.call(self.ts, name, args)
        await self.settle()
        return res

    def outcomes(self, start: int = 0) -> list[str]:
        """What each queued job ended with, as the queue logged it."""
        out = []
        for r in self.records[start:]:
            msg = r.getMessage()
            if r.exc_info:
                exc = r.exc_info[1]
                msg += f" -- raised {type(exc).__name__}: {exc}"
            out.append(msg)
        return out

    def failures(self, start: int = 0) -> list[str]:
        return [r.getMessage() for r in self.records[start:] if r.levelno >= logging.WARNING]

    def imu(self, tilt_deg: float, spin: float = 0.0, seconds: float = 1.5) -> None:
        """Feed IMU readings: tilted tilt_deg about x, turning at spin rad/s."""
        h = math.radians(tilt_deg) / 2
        for _ in range(int(seconds * 100)):
            self.robot.send("/imu_sensor_broadcaster/imu", _Msg(
                orientation=_Quat(x=math.sin(h), w=math.cos(h)), angular_velocity=_Vec(x=spin)))
            self.clock.now += 0.01

    def close(self):
        self.ts.worker.cancel()


# ----------------------------------------------------------------------------- checks
PASS = FAIL = TODO = 0


def check(name: str, ok: bool, detail: str = "", passes: str = "") -> bool:
    """One check. On a failure, print what we got (detail) and an example that passes."""
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f": {detail}" if detail else ""))
        if passes:
            print(f"         passes, e.g.: {passes}")
    return ok


def unwritten(res) -> str | None:
    """'TODO n' if a result (or a check_move reason) says that TODO isn't written yet."""
    text = res.message if hasattr(res, "message") else str(res or "")
    m = re.search(r"(TODO \d+) is not written yet", text)
    return m.group(1) if m else None


def skip(what: str) -> None:
    global TODO
    TODO += 1
    print(f"  --   {what}")


def walk_twists(rig: Rig):
    return [m for _, m in rig.robot.twists() if abs(m.linear.x) + abs(m.linear.y) + abs(m.angular.z) > 1e-9]


def described(rig: Rig, names: list[str], todo: str) -> bool:
    """Check the declarations of some tools. False (and a skip) if one still says TODO."""
    decls = {d["name"]: d for d in tools.declarations(rig.ts)}
    missing = [n for n in names if n not in decls]
    if missing:
        check(f"lab/ declares {', '.join(names)}", False, f"missing {missing}; declared: {sorted(decls)}")
        return False
    if any("TODO" in json.dumps(decls[n]) for n in names):
        skip(f"{todo}: the descriptions of {', '.join(names)} still say TODO")
        return False
    for n in names:
        d = decls[n]
        check(f"{n} has a real description (at least 20 characters)", len(d.get("description", "")) >= 20,
              f"description: {d.get('description')!r}",
              passes='"Turn in place to the left or right." (a bare "turn" fails)')
        params = d.get("parameters", {}).get("properties", {})
        no_doc = [p for p, s in params.items() if len(s.get("description", "")) < 3]
        check(f"... and every parameter of {n} is described", not no_doc, f"missing: {no_doc}",
              passes='angle=Param("number", "Degrees to turn, e.g. 90. Default 90.")')
    return True


def system_prompt() -> str:
    text = (HERE.parent / "lab" / "prompt.md").read_text()
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


# ----------------------------------------------------------------------------- Part 1
def prompt_sections(*headings: str) -> str:
    """The text under some of prompt.md's '# ' headings."""
    parts, keep = [], False
    for line in system_prompt().splitlines():
        if line.startswith("# "):
            keep = line[2:].strip().lower() in {h.lower() for h in headings}
        elif keep:
            parts.append(line)
    return "\n".join(parts).strip()


async def todo1() -> None:
    print("TODO 1: prompt.md, first pass")
    text = prompt_sections("Who you are", "How you move", "When a tool says no")
    if "TODO" in text or not text:
        skip("TODO 1: prompt.md still says TODO under its first three headings")
        return
    words = len(text.split())
    check("the prompt says who Pupper is and how it moves (at least 60 words in the three sections)", words >= 60,
          f"{words} words", passes="a few bullet points under each heading, about 60 words or more in all")
    check('it tells Gemini it moves only by calling tools (says "tool")', "tool" in text.lower(),
          passes='"You can only move by calling a tool." (fails: "You can walk and turn.")')


# ----------------------------------------------------------------------------- Part 2
async def todo2() -> None:
    print("TODO 2: check_move refuses what the robot can't or shouldn't do")
    probe = check_move(0.4, 0.0, 0.0, 2.0)
    if unwritten(probe):
        skip("TODO 2 (check_move) is not written yet")
        return
    L = LIMITS
    fine = [
        ("walk forward 0.4 m/s for 2 s", (0.4, 0.0, 0.0, 2.0)),
        ("walk backward 0.4 m/s for 2 s", (-0.4, 0.0, 0.0, 2.0)),
        ("sidestep 0.3 m/s", (0.0, 0.3, 0.0, 2.0)),
        ("turn in place 60 deg/s", (0.0, 0.0, 60.0, 1.5)),
        ("run 1.2 m/s for 4 s (4.8 m)", (1.2, 0.0, 0.0, 4.0)),
        ("the fastest allowed forward speed", (L.vx_max, 0.0, 0.0, 1.0)),
    ]
    for name, args in fine:
        got = check_move(*args)
        check(f"allows: {name}", got is None, f"check_move{args} refused it: {got!r}",
              passes="return None for a move inside every limit (exactly at a limit is still allowed)")
    bad = [
        ("forward faster than vx_max", (L.vx_max + 1.0, 0.0, 0.0, 1.0), str(L.vx_max)),
        ("backward faster than vx_max", (-(L.vx_max + 1.0), 0.0, 0.0, 1.0), str(L.vx_max)),
        ("sideways faster than vy_max", (0.0, L.vy_max + 0.3, 0.0, 1.0), str(L.vy_max)),
        ("turning faster than wz_max", (0.0, 0.0, L.wz_max + 50, 1.0), str(L.wz_max)),
        ("too slow to move at all", (0.05, 0.05, 5.0, 2.0), None),
        ("a zero duration", (0.4, 0.0, 0.0, 0.0), None),
        ("a negative duration", (0.4, 0.0, 0.0, -1.0), None),
        ("longer than duration_max", (0.4, 0.0, 0.0, L.duration_max + 1), str(L.duration_max)),
        ("farther than distance_max in one move", (1.5, 0.0, 0.0, 6.0), str(L.distance_max)),
    ]
    for name, args, limit in bad:
        got = check_move(*args)
        if check(f"refuses: {name}", isinstance(got, str) and got != "", f"check_move{args} returned {got!r}",
                 passes='return a sentence saying what is wrong, e.g. "duration must be more than 0"'):
            if limit is not None:
                check(f"  ... and the reason includes the limit {limit} (so Gemini knows what to try instead)",
                      limit in got, f"reason was {got!r}: add the limit's number",
                      passes=f'"... {limit} ..." from {{limits.<field>}} in an f-string; fails: a reason with no number'
                      + (f', or "{limit[:-2]}" ({limit} rounded)' if limit.endswith(".0") else ""))
    await provided_walk()


async def provided_walk() -> None:
    print("  (provided) walk, with your check_move: sends the speed for the whole move, then stands still")
    rig = Rig()
    try:
        res = await behaviors.walk(rig.ts, 5.0, 0.0, 0.0, 1.0)
        if unwritten(res):
            skip(f"{unwritten(res)} is not written yet")
            return
        check("an impossible speed comes back as a failure", res.ok is False, f"got {res}")
        check("... and Gemini hears your check_move reason, limit included (1.5)", "1.5" in res.message,
              f"got {res.message!r}", passes='"too fast: limits are |vx|<=1.5 m/s, ..." (fix it in check_move: TODO 2)')
        check("... and nothing was queued or published",
              rig.ts.commands.empty() and not rig.robot.twists(), f"published {len(rig.robot.twists())} commands")

        rig.robot.controllers[LOCOMOTION_CONTROLLER] = "inactive"  # lying down
        start = len(rig.records)
        res = await behaviors.walk(rig.ts, 0.4, 0.0, 0.0, 2.0)
        check("a good move is accepted right away (queued)", res.ok is True and "queued" in res.message, f"got {res}")
        if not await rig.settle():
            check("the queue finishes", False, "the move never ended")
            return
        crashed = [o for o in rig.outcomes(start) if "raised" in o]
        check("the job ran without crashing", not crashed, "; ".join(crashed))
        check("the locomotion policy took the legs", LOCOMOTION_CONTROLLER in rig.robot.active())
        twists = rig.robot.twists()
        moving = [m for _, m in twists if abs(m.linear.x) > 1e-6]
        check("it sent the forward speed", bool(moving) and all(abs(m.linear.x - 0.4) < 1e-6 for m in moving),
              f"forward speeds sent: {sorted({round(m.linear.x, 3) for m in moving})}")
        if twists:
            span = twists[-1][0] - twists[0][0]
            check("for about the requested 2 s", 1.8 <= span <= 2.5, f"commands spanned {span:.2f} s")
            gaps = [b[0] - a[0] for a, b in zip(twists, twists[1:])]
            check("often enough that cmd_vel_mux never drops it (every < 0.5 s)",
                  bool(gaps) and max(gaps) < 0.5, f"longest gap {max(gaps) if gaps else 'n/a'} s")
            last = twists[-1][1]
            check("and ends with a zero command (stand still)",
                  abs(last.linear.x) + abs(last.linear.y) + abs(last.angular.z) < 1e-9)
            check("at roughly LLM_CMD_RATE", len(twists) >= 0.8 * 2.0 * LLM_CMD_RATE, f"{len(twists)} commands in 2 s")
        check("the outcome (in the debug log) says what it did", any("moved" in o for o in rig.outcomes(start)),
              f"log: {rig.outcomes(start)}")
        rig.robot.published.clear()
        await behaviors.walk(rig.ts, 0.0, 0.0, 60.0, 1.0)
        await rig.settle()
        turns = [m for _, m in rig.robot.twists() if abs(m.angular.z) > 1e-6]
        check("a turn sends its turn rate in rad/s", bool(turns) and abs(turns[0].angular.z - 60 / 57.2958) < 1e-3,
              f"turn rates sent: {sorted({round(m.angular.z, 3) for m in turns})}")

        # Falling over in the middle of a walk.
        rig.robot.published.clear()
        start = len(rig.records)
        await behaviors.walk(rig.ts, 0.4, 0.0, 0.0, 4.0)
        t0 = rig.clock.now
        while rig.clock.now < t0 + 1.0:
            await _real_sleep(0)
        rig.ts.fallen = LOCOMOTION_CONTROLLER  # as if the tilt e-stop had just tripped
        await rig.settle()
        twists = rig.robot.twists()
        check("if the robot falls while walking, it stops sending the speed",
              bool(twists) and twists[-1][0] - t0 < 2.0, f"kept sending until {twists[-1][0] - t0 if twists else 0:.1f} s")
        check("... ends on a zero command", bool(twists) and abs(twists[-1][1].linear.x) < 1e-9)
        check("... and the outcome says it fell", any("fell" in o for o in rig.outcomes(start)),
              f"log: {rig.outcomes(start)}")
        res = await behaviors.walk(rig.ts, 0.4, 0.0, 0.0, 1.0)
        check("a walk asked for while fallen is refused right away, saying so",
              not res.ok and "fell" in res.message, f"got {res}")
    finally:
        rig.close()


async def todo3() -> None:
    print("TODO 3: the turn tool, then the move tool")
    rig = Rig()
    try:
        # turn first: the warm-up.
        described(rig, ["turn"], "TODO 3 (turn)")
        res = await rig.call("turn", direction="left", angle=90)
        if unwritten(res):
            skip(f"{unwritten(res)} is not written yet (the turn tool needs TODO 2 and TODO 3)")
            return
        twists = rig.robot.twists()
        yaw = sum(m.angular.z * (b[0] - a[0]) for (a, b), m in zip(zip(twists, twists[1:]), (t[1] for t in twists)))
        check("turn left turns counter-clockwise (+z)", any(m.angular.z > 0 for _, m in twists),
              f"result {res}; turn rates sent: {sorted({round(m.angular.z, 2) for _, m in twists})}",
              passes="walk(robot, 0.0, 0.0, +TURN_SPEED, ...) for left (fails: -TURN_SPEED)")
        check("... by about the angle asked for (90 deg)", 70 <= math.degrees(yaw) <= 110,
              f"turned about {math.degrees(yaw):.0f} deg", passes="seconds = abs(angle) / TURN_SPEED: 90 deg -> 1.5 s")
        check("... in place (no forward or sideways speed)",
              all(abs(m.linear.x) + abs(m.linear.y) < 1e-9 for _, m in twists), passes="vx = vy = 0.0")
        rig.robot.published.clear()
        await rig.call("turn", direction="right", angle=90)
        twists = rig.robot.twists()
        check("turn right turns clockwise (-z)", any(m.angular.z < 0 for _, m in twists),
              f"turn rates sent: {sorted({round(m.angular.z, 2) for _, m in twists})}", passes="wz_deg = -TURN_SPEED")

        # move: the real challenge.
        if described(rig, ["move"], "TODO 3 (move)"):
            text = json.dumps({d["name"]: d for d in tools.declarations(rig.ts)}["move"])
            check(f"move's words give the real speed limit (LIMITS.vx_max, {LIMITS.vx_max})", str(LIMITS.vx_max) in text,
                  passes='f"... running {RUN_MIN_SPEED}-{LIMITS.vx_max} m/s ..." (fails: "fast", or a typed-in 1.5)')
            check('... and say that it ends follow mode (says "follow")', "follow" in text.lower(),
                  passes='"Walk or run ..." + PREPARES_MOVE (fails: leaving PREPARES_MOVE out)')
        rig.robot.published.clear()
        res = await rig.call("move", direction="forward", duration=1.0)
        if unwritten(res):
            skip(f"{unwritten(res)} is not written yet (the move tool)")
            return

        async def sent(**args):
            rig.robot.published.clear()
            res = await rig.call("move", duration=1.0, **args)
            return res, walk_twists(rig)

        res, ts = await sent(direction="forward")
        check('move("forward") walks forward at the default 0.4 m/s',
              bool(ts) and all(abs(m.linear.x - 0.4) < 1e-6 and abs(m.linear.y) < 1e-9 for m in ts),
              f"result {res}; sent vx {sorted({round(m.linear.x, 2) for m in ts})}", passes="vx = 0.4, vy = 0")
        res, ts = await sent(direction="backward", speed=0.3)
        check('move("backward", speed=0.3) sends vx = -0.3', bool(ts) and all(abs(m.linear.x + 0.3) < 1e-6 for m in ts),
              f"sent vx {sorted({round(m.linear.x, 2) for m in ts})}", passes="vx = -speed (fails: +0.3)")
        res, ts = await sent(direction="left", speed=0.3)
        check('move("left", speed=0.3) sends vy = +0.3 (the robot\'s left is +y)',
              bool(ts) and all(abs(m.linear.y - 0.3) < 1e-6 and abs(m.linear.x) < 1e-9 for m in ts),
              f"sent vy {sorted({round(m.linear.y, 2) for m in ts})}", passes="vy = +speed, vx = 0 (fails: vy = -0.3)")
        res, ts = await sent(direction="backward", run=True)
        check('move("backward", run=True) runs at RUN_DEFAULT_SPEED (vx = -1.2)',
              bool(ts) and all(abs(m.linear.x + 1.2) < 1e-6 for m in ts), f"sent vx {sorted({round(m.linear.x, 2) for m in ts})}",
              passes="vx = -1.2 (fails: -0.4, the walking default)")
        res, ts = await sent(direction="forward", run=True, speed=0.6)
        check('move("forward", run=True, speed=0.6) still runs: at least 1.0 m/s',
              bool(ts) and min(m.linear.x for m in ts) >= 1.0 - 1e-9, f"sent vx {sorted({round(m.linear.x, 2) for m in ts})}",
              passes="vx = max(0.6, RUN_MIN_SPEED) = 1.0 (fails: 0.6)")
        res, ts = await sent(direction="right", run=True)
        check('move("right", run=True) sidesteps at walking speed (vy = -0.4): sideways never runs',
              bool(ts) and all(abs(m.linear.y + 0.4) < 1e-6 for m in ts), f"result {res}; sent vy {sorted({round(m.linear.y, 2) for m in ts})}",
              passes="vy = -0.4 (fails: -1.2, running sideways)")
        check('... and says so: the reply says "walk"', res.ok and "walk" in res.message.lower(), f"got {res}",
              passes='"Sideways never runs; walking instead. move ... queued" (fails: just "move ... queued")')
        res = await rig.call("move", direction="forward", speed=1.5, duration=10.0)
        check("a move the robot refuses comes back as a failure with the reason",
              res.ok is False and "m" in res.message, f"got {res}",
              passes='Result(False, "that would travel 15.00 m, more than 7.0 m in one move: split it into shorter moves") from walk -> check_move')
    finally:
        rig.close()


async def todo4() -> None:
    print("TODO 4: the arc tool (walk and turn at the same time)")
    rig = Rig()
    try:
        if described(rig, ["arc"], "TODO 4"):
            text = json.dumps({d["name"]: d for d in tools.declarations(rig.ts)}["arc"])
            check(f"arc's words give how far one arc can go (LIMITS.distance_max, {LIMITS.distance_max})",
                  str(LIMITS.distance_max) in text,
                  passes='f"... One arc goes at most {LIMITS.distance_max} m." (fails: "Walk in a curve.")')
            check('... and say that it ends follow mode (says "follow")', "follow" in text.lower(),
                  passes='"Walk forward and turn ..." + PREPARES_MOVE (fails: leaving PREPARES_MOVE out)')
        res = await rig.call("arc", direction="left", angle=90, radius=0.5)
        if unwritten(res):
            skip(f"{unwritten(res)} is not written yet (arc needs TODO 2 and TODO 4)")
            return

        def swept(ts):
            return math.degrees(sum(m.angular.z * (b[0] - a[0]) for (a, b), m in zip(zip(ts, ts[1:]), (t[1] for t in ts))))

        twists = rig.robot.twists()
        moving = walk_twists(rig)
        wz = 45.84  # math.degrees(0.4 / 0.5)
        check('arc("left", angle=90, radius=0.5) goes forward at the default 0.4 m/s and turns left',
              bool(moving) and all(abs(m.linear.x - 0.4) < 1e-6 and abs(m.linear.y) < 1e-9 for m in moving)
              and all(m.angular.z > 0 for m in moving),
              f"result {res}; sent vx {sorted({round(m.linear.x, 2) for m in moving})},"
              f" wz {sorted({round(math.degrees(m.angular.z), 1) for m in moving})} deg/s",
              passes="walk(robot, 0.4, 0.0, +45.8, ...) (fails: vx = 0, a turn in place)")
        check(f"... at wz = speed / radius = {wz:.1f} deg/s", bool(moving) and all(abs(math.degrees(m.angular.z) - wz) < 0.5 for m in moving),
              f"sent wz {sorted({round(math.degrees(m.angular.z), 1) for m in moving})} deg/s",
              passes="wz_deg = math.degrees(speed / radius) (fails: speed / radius = 0.8, which is rad/s)")
        check("... for about 90 deg of the circle", 70 <= swept(twists) <= 110, f"turned about {swept(twists):.0f} deg",
              passes="seconds = abs(angle) / abs(wz_deg) = 1.96 s")
        rig.robot.published.clear()
        res = await rig.call("arc", direction="right", angle=90, radius=0.5)
        moving = walk_twists(rig)
        check('arc("right", ...) turns right (wz < 0), still going forward (vx > 0)',
              bool(moving) and all(m.angular.z < 0 and m.linear.x > 0 for m in moving),
              f"result {res}; sent vx {sorted({round(m.linear.x, 2) for m in moving})},"
              f" wz {sorted({round(math.degrees(m.angular.z), 1) for m in moving})}",
              passes="wz_deg = -45.8, vx = +0.4 (fails: vx = -0.4, which backs up)")
        rig.robot.published.clear()
        res = await rig.call("arc", direction="left", angle=90, speed=0.6, radius=1.0)
        moving = walk_twists(rig)
        check('arc("left", speed=0.6, radius=1.0) sends vx = 0.6, wz = +34.4 deg/s',
              bool(moving) and all(abs(m.linear.x - 0.6) < 1e-6 and abs(math.degrees(m.angular.z) - 34.38) < 0.5 for m in moving),
              f"result {res}; sent vx {sorted({round(m.linear.x, 2) for m in moving})},"
              f" wz {sorted({round(math.degrees(m.angular.z), 1) for m in moving})}",
              passes="vx = speed, wz_deg = math.degrees(0.6 / 1.0)")
        rig.robot.published.clear()
        res = await rig.call("arc", direction="left", angle=90, radius=0)
        twists = rig.robot.twists()
        moving = walk_twists(rig)
        check('arc("left", angle=90, radius=0) turns in place, left, about 90 deg (a sharp corner)',
              getattr(res, "ok", False) and bool(moving) and all(abs(m.linear.x) + abs(m.linear.y) < 1e-9 and m.angular.z > 0 for m in moving)
              and 70 <= swept(twists) <= 110, f"got {res}; turned about {swept(twists):.0f} deg",
              passes="walk(robot, 0.0, 0.0, +TURN_SPEED, abs(angle) / TURN_SPEED) (fails: a crash dividing by 0, or a refusal)")
        check('... and the reply says so (says "place")', getattr(res, "ok", False) and "place" in res.message.lower(), f"got {res}",
              passes='"Radius 0: turned in place (a sharp corner). move vx=0 ... queued"')
        rig.robot.published.clear()
        res = await rig.call("arc", direction="left", angle=90, radius=-1)
        check('arc("left", radius=-1) is a failure that says "radius", and sends nothing',
              res.ok is False and "radius" in res.message.lower() and not walk_twists(rig), f"got {res}",
              passes='Result(False, "no arc: radius must be 0 m or more ...")')
        rig.robot.published.clear()
        res = await rig.call("arc", direction="left", angle=90, radius=0.1)
        nums = [float(x) for x in re.findall(r"\d+\.\d+|\d+", res.message)] if not res.ok else []
        check('a too-tight arc (radius 0.1 m at 0.4 m/s) fails, says "radius", and gives the smallest radius (0.2-0.3 m)',
              res.ok is False and "radius" in res.message.lower() and any(0.2 <= n <= 0.3 for n in nums) and not walk_twists(rig),
              f"got {res}",
              passes='"too tight: at 0.4 m/s the radius must be at least 0.25 m" (fails: check_move\'s'
                     ' "too fast: ... |wz|<=114.0": Gemini can\'t tell the radius is the problem)')
    finally:
        rig.close()


# ----------------------------------------------------------------------------- Part 3
async def todo5() -> None:
    print("TODO 5: any motion ends follow mode right away, and Gemini hears about it")
    rig = Rig()
    try:
        res = await rig.ts.set_following(True)
        await rig.settle()
        check("(provided) following starts", rig.ts.following and rig.robot.follower_on, f"got {res}")
        res = await behaviors.stand_up(rig.ts)
        stopped_now = not rig.ts.following and not rig.robot.follower_on
        if not stopped_now and rig.robot.follow_calls == ["on"] and "follow" not in res.message.lower():
            skip("TODO 5 (prepare_to_move) is not written yet")
            return
        check("standing up ends follow mode right away (before the queue reaches it)", stopped_now,
              passes="await robot.stop_following() in prepare_to_move (fails: doing it inside a job)")
        check('the tool result tells Gemini it stopped following (says "follow")', "follow" in res.message.lower(),
              f"result was {res.message!r}",
              passes='"Stopped following the person first. stand up queued; ..." (fails: "stand up queued; ...")')
        await rig.settle()
        await rig.ts.set_following(True)
        await rig.settle()
        res = await behaviors.lie_down(rig.ts)
        check("lying down ends follow mode too", not rig.ts.following and not rig.robot.follower_on)
        check('... and says so (says "follow")', "follow" in res.message.lower(), f"result was {res.message!r}",
              passes='"Stopped following the person first. lie down queued; ..."')
        await rig.settle()
        await rig.ts.set_following(True)
        await rig.settle()
        res = await behaviors.walk(rig.ts, 0.4, 0.0, 0.0, 1.0)
        if unwritten(res):
            print(f"  --   (skipping the walking case: {unwritten(res)} is not written yet)")
        else:
            check("walking ends follow mode", not rig.ts.following and not rig.robot.follower_on)
            check('... and says so (says "follow")', "follow" in res.message.lower(), f"result was {res.message!r}",
                  passes='"Stopped following the person first. move vx=0.4 ... queued; ..."')
        await rig.settle()
        res = await behaviors.stand_up(rig.ts)
        check("with follow mode off, results don't mention following", "follow" not in res.message.lower(),
              f"result was {res.message!r}",
              passes='"stand up queued; ..." (fails: "Stopped following ..." when it wasn\'t following: return "")')
        await rig.settle()
        await rig.ts.set_following(True)
        await rig.settle()
        await rig.ts.mode()
        check("asking what mode it is in keeps following", rig.ts.following and rig.robot.follower_on,
              passes="only motions call prepare_to_move; questions (robot_status) don't")
    finally:
        rig.close()


async def todo6() -> None:
    print("TODO 6: the follow tool")
    rig = Rig()
    try:
        if described(rig, ["follow"], "TODO 6"):
            text = json.dumps({d["name"]: d for d in tools.declarations(rig.ts)}["follow"]).lower()
            check('follow\'s description says what ends follow mode (says "end" or "stop")', "end" in text or "stop" in text,
                  passes='"... Talking keeps following; any motion tool ends it." (fails: "Follow the person.")')
        follow_text = prompt_sections("Follow mode")
        if "TODO" in follow_text or not follow_text:
            skip("TODO 6: prompt.md still says TODO under Follow mode")
        else:
            check('the prompt\'s Follow mode section says what ends following (says "end" or "stop")',
                  "end" in follow_text.lower() or "stop" in follow_text.lower(), follow_text,
                  passes='"- Any motion ends follow mode. Before you call that tool, say so."')
        res = await rig.call("follow", enabled=True)
        if unwritten(res):
            skip(f"{unwritten(res)} is not written yet")
            return
        check("follow(enabled=True) starts following", rig.robot.follower_on, f"got {res}",
              passes="return await robot.set_following(enabled)")
        res = await rig.call("follow", enabled=False)
        check("follow(enabled=False) stops it", not rig.robot.follower_on, f"got {res}",
              passes="return await robot.set_following(enabled) (fails: always passing True)")
    finally:
        rig.close()


# ----------------------------------------------------------------------------- Part 4
async def todo7() -> None:
    print("TODO 7: play_trick (the fake tricks: bow, roll_over, hold_pose)")
    rig = Rig()
    try:
        res = await behaviors.play_trick(rig.ts, "backflip")
        await rig.settle()
        if unwritten(res):
            skip("TODO 7 (play_trick) is not written yet")
            return
        check("an unknown trick fails", not res.ok, f"got {res}", passes="Result(False, ...) when robot.tricks.get(name) is None")
        check("... and the message lists the tricks that exist", "bow" in res.message, f"got {res.message!r}",
              passes='"unknown trick \'backflip\'; available: [\'bow\', \'roll_over\', ...]" (fails: "unknown trick")')

        bow = rig.ts.tricks["bow"]
        start = len(rig.records)
        res = await behaviors.play_trick(rig.ts, "bow")
        check("a trick is accepted right away (queued)", res.ok and "queued" in res.message, f"got {res}",
              passes='return what robot.queue(...) returns: "trick bow queued; ..." (fails: awaiting the job yourself)')
        await rig.settle()
        if any(unwritten(_Msg(message=o)) for o in rig.outcomes(start)):
            skip("TODO 7 (play_policy_trick, the job) is not written yet")
            return
        on, off = rig.robot.activations(bow.controller), rig.robot.deactivations(bow.controller)
        check("the trick's controller took the legs", bool(on), f"switches: {rig.robot.switches}",
              passes="job = lambda: play_policy_trick(robot, trick), queued")
        check("... for the whole trick (trick.total_time)", bool(on and off) and off[-1] - on[-1] >= bow.total_time,
              f"held it {off[-1] - on[-1] if on and off else 0:.1f} s of {bow.total_time:.1f} s",
              passes="the provided play_policy_trick as the job (it waits trick.total_time)")
        check("a trick that ends standing hands the legs back to walking",
              LOCOMOTION_CONTROLLER in rig.robot.active() and bow.controller not in rig.robot.active(),
              f"active: {sorted(rig.robot.active())}", passes="the provided play_policy_trick as the job")
        check('... and the outcome says so (says "locomotion")', any("locomotion" in o for o in rig.outcomes(start)),
              f"log: {rig.outcomes(start)}", passes='"played bow; back in locomotion mode"')

        roll = rig.ts.tricks["roll_over"]
        start = len(rig.records)
        await behaviors.play_trick(rig.ts, "roll_over")
        await rig.settle()
        check("a trick that ends lying down keeps its controller holding the pose",
              roll.controller in rig.robot.active() and LOCOMOTION_CONTROLLER not in rig.robot.active(),
              f"active: {sorted(rig.robot.active())}", passes="the provided play_policy_trick as the job")
        check('... and the outcome says it holds the final pose (says "hold" or "pose")',
              any("hold" in o or "pose" in o for o in rig.outcomes(start)), f"log: {rig.outcomes(start)}",
              passes='"played roll_over; it holds its final pose (lying)"')
        n_on = len(rig.robot.activations(roll.controller))
        await behaviors.play_trick(rig.ts, "roll_over")
        await rig.settle()
        check("playing it again restarts its controller (relax, then switch to it)",
              len(rig.robot.activations(roll.controller)) == n_on + 1 and len(rig.robot.deactivations(roll.controller)) >= 1,
              f"switches: {rig.robot.switches[-3:]}", passes="the provided play_policy_trick as the job (its step 2)")

        hold = rig.ts.tricks["hold_pose"]
        start = len(rig.records)
        res = await behaviors.play_trick(rig.ts, "hold_pose")
        await rig.settle()
        check("an interactive trick: Pupper stays in it", hold.controller in rig.robot.active() and rig.ts.state_now is hold,
              f"active: {sorted(rig.robot.active())}; state_now: {rig.ts.state_now}",
              passes="the provided play_policy_trick as the job")
        out = " ".join(rig.outcomes(start))
        check("... and the outcome lists its actions and its exit clip", "wave_left" in out and "leave" in out,
              f"log: {rig.outcomes(start)}",
              passes='"played hold_pose; it now stays ... Interactive actions: wave_left, wave_right. Any other motion first plays leave."')
        n_switches = len(rig.robot.switches)
        start = len(rig.records)
        await behaviors.play_trick(rig.ts, "hold_pose")
        await rig.settle()
        check("asking for the interactive trick Pupper is already in does nothing, and says so",
              len(rig.robot.switches) == n_switches and any("already" in o for o in rig.outcomes(start)),
              f"log: {rig.outcomes(start)}",
              passes='"already doing hold_pose" (needs then=trick for an interactive trick; fails: then=LOCOMOTION)')

    finally:
        rig.close()

    rig = Rig()
    try:
        await rig.ts.set_following(True)
        await rig.settle()
        res = await behaviors.play_trick(rig.ts, "bow")
        await rig.settle()
        check('a trick ends follow mode (TODO 5\'s rule), and says so (says "follow")',
              not rig.ts.following and "follow" in res.message.lower(), f"got {res}",
              passes='with_note(note, ...): "Stopped following the person first. trick bow queued; ..."')
    finally:
        rig.close()


async def todo8() -> None:
    print("TODO 8: the play_trick tool")
    rig = Rig()
    try:
        if not described(rig, ["play_trick"], "TODO 8"):
            return
        decl = {d["name"]: d for d in tools.declarations(rig.ts)}["play_trick"]
        check("its description lists every trick with its own description (has {tricks})",
              all(t.description in decl["description"] for t in rig.ts.tricks.values()), decl["description"],
              passes='"Play a trick. ..." + PREPARES_MOVE + "\\nTricks:\\n{tricks}" (fails: listing the tricks by hand)')
        params = decl.get("parameters", {}).get("properties", {})
        enums = [v for p in params.values() for v in p.get("enum", [])]
        check("its choices are the installed tricks", set(rig.ts.tricks) <= set(enums), f"choices: {enums}",
              passes="name=Param(\"string\", ..., enum=TRICK_NAMES) (keep the enum)")
        check('... and it says a trick keeps its final pose (says "pose")',
              "pose" in decl["description"].lower(), decl["description"],
              passes='"A trick keeps its final pose until the next motion or stand_up."')
    finally:
        rig.close()


async def optional_jump() -> None:
    print("Optional: the jump (the fake reference trick 'hop')")
    rig = Rig()
    try:
        res = await behaviors.play_trick(rig.ts, "hop")
        await rig.settle()
        if unwritten(res):
            skip("TODO 7 (play_trick) is not written yet")
            return
        refs = [m for _, topic, m in rig.robot.published if topic.endswith("/reference_trick")]
        check("a reference trick plays through the locomotion policy (robot.play_reference_trick)",
              bool(refs) and LOCOMOTION_CONTROLLER in rig.robot.active(),
              f"got {res}; log: {rig.outcomes()}; active: {sorted(rig.robot.active())}",
              passes='trick.kind == "reference" -> job = lambda: robot.play_reference_trick(trick)')
    finally:
        rig.close()


# ----------------------------------------------------------------------------- Part 5
async def todo9() -> None:
    print("TODO 9: interactive actions (the fake interactive trick 'hold_pose': wave_left, wave_right, leave)")
    rig = Rig()
    try:
        ctrl = rig.ts.tricks["hold_pose"].controller
        res = await behaviors.interactive_action(rig.ts, "wave_left")
        await rig.settle()
        if unwritten(res):
            skip("TODO 9 (interactive_action) is not written yet")
            return
        probe = await behaviors.play_trick(rig.ts, "bow")
        await rig.settle()
        if unwritten(probe) or any(unwritten(_Msg(message=o)) for o in rig.outcomes()):
            skip("these checks need TODO 7 (play_trick) first")
            return

        start = len(rig.records)
        res = await behaviors.interactive_action(rig.ts, "fetch_the_ball")
        await rig.settle()
        fails = ([] if res.ok else [res.message]) + rig.failures(start)
        check("an unknown action fails", bool(fails), f"got {res}",
              passes="Result(False, ...) when robot.action_ids().get(action) is None")
        check("... and the message lists the actions that exist", any("wave_left" in f for f in fails),
              f"messages: {fails}",
              passes='"unknown interactive action fetch_the_ball; available: [\'wave_left\', ...]" (fails: "unknown action")')

        start = len(rig.records)
        rig.robot.published.clear()
        res = await behaviors.interactive_action(rig.ts, "wave_left")  # hold_pose is not running
        await rig.settle()
        check("an action while its trick is not running fails right away", not res.ok, f"got {res}",
              passes="refuse now if robot.state_after_queue is not trick (fails: only checking in the job)")
        check("... the message says which trick has to run first", "hold_pose" in res.message, f"got {res.message!r}",
              passes='"wave_left only works while hold_pose is running; play it first (play_trick hold_pose)"')
        check("... and nothing was sent to the trick", not rig.robot.clips(ctrl), passes="return before robot.queue")

        await behaviors.play_trick(rig.ts, "hold_pose")
        await rig.settle()
        start = len(rig.records)
        res = await behaviors.interactive_action(rig.ts, "wave_left")
        await rig.settle()
        check("an action while the trick holds its pose is accepted", res.ok, f"got {res}",
              passes='"hold_pose wave_left queued; ..."')
        check("... and plays that clip", rig.robot.clips(ctrl)[-1:] == ["wave_left"], f"clips sent: {rig.robot.clips(ctrl)}",
              passes="await robot.play_clip(trick, clip) in the job")
        check("... without a failure in the queue", not rig.failures(start), f"{rig.failures(start)}",
              passes="the job returns play_clip's Result")
        check("... and Pupper stays in the trick afterwards",
              ctrl in rig.robot.active() and LOCOMOTION_CONTROLLER not in rig.robot.active(),
              passes="no switch_to after a clip that isn't the exit clip")

        await behaviors.interactive_action(rig.ts, "leave")
        await rig.settle()
        check("the exit clip plays", rig.robot.clips(ctrl)[-1:] == ["leave"], f"clips sent: {rig.robot.clips(ctrl)}",
              passes="await robot.switch_to(LOCOMOTION_CONTROLLER): it plays the exit clip on the way out")
        check("... then the locomotion policy takes the legs back",
              LOCOMOTION_CONTROLLER in rig.robot.active() and ctrl not in rig.robot.active(),
              f"active: {sorted(rig.robot.active())}",
              passes="for the exit clip: await robot.switch_to(LOCOMOTION_CONTROLLER)")
        check("... and Pupper is in locomotion mode again", rig.ts.state_now == LOCOMOTION,
              f"state_now: {rig.ts.state_now}", passes="switch_to keeps robot.state_now up to date")
        res = await behaviors.interactive_action(rig.ts, "wave_left")
        check("after the exit clip, actions are refused again", not res.ok, f"got {res}",
              passes="then=LOCOMOTION when queuing the exit clip (fails: then=trick)")
    finally:
        rig.close()

    rig = Rig()
    try:
        ctrl = rig.ts.tricks["hold_pose"].controller
        # "Sit and give me your paw", in one breath: two tool calls, back to back.
        await behaviors.play_trick(rig.ts, "hold_pose")
        start = len(rig.records)
        res = await behaviors.interactive_action(rig.ts, "wave_right")
        await rig.settle()
        check("an action queued right behind its trick is accepted and plays",
              res.ok and "wave_right" in rig.robot.clips(ctrl),
              f"result {res}; queue: {rig.failures(start)}; clips sent: {rig.robot.clips(ctrl)}",
              passes="the now-check asks robot.state_after_queue (fails: asking robot.state_now or is_active now)")

        # Queued behind the trick, but the trick was relaxed before the queue got there.
        await behaviors.play_trick(rig.ts, "hold_pose")
        await rig.settle()
        await behaviors.lie_down(rig.ts)  # queued: relaxes the motors
        rig.ts.state_after_queue = rig.ts.tricks["hold_pose"]  # as if it were still coming
        rig.robot.published.clear()
        start = len(rig.records)
        res = await behaviors.interactive_action(rig.ts, "wave_left")
        await rig.settle()
        check("when the queue gets to an action, it checks again that its trick is running",
              not rig.robot.clips(ctrl) and bool(rig.failures(start)),
              f"clips sent: {rig.robot.clips(ctrl)}; log: {rig.outcomes(start)}",
              passes="in the job, first: if robot.state_now is not trick: return Result(False, ...)")
    finally:
        rig.close()


async def todo10() -> None:
    print("TODO 10: the interactive_action tool")
    rig = Rig()
    try:
        if described(rig, ["interactive_action"], "TODO 10"):
            decl = {d["name"]: d for d in tools.declarations(rig.ts)}["interactive_action"]
            check("its description lists the actions (has {actions})", "Waves the left paw." in decl["description"],
                  decl["description"], passes='"Do something inside a trick ... Actions:\\n{actions}"')
            params = decl.get("parameters", {}).get("properties", {})
            enums = [v for p in params.values() for v in p.get("enum", [])]
            check("its choices are the installed actions", {"wave_left", "wave_right", "leave"} <= set(enums),
                  f"choices: {enums}", passes="action=Param(\"string\", ..., enum=ACTION_IDS) (keep the enum)")
        ctrl = rig.ts.tricks["hold_pose"].controller
        await behaviors.play_trick(rig.ts, "hold_pose")
        await rig.settle()
        res = await rig.call("interactive_action", action="wave_left")
        if unwritten(res):
            skip(f"{unwritten(res)} is not written yet")
            return
        check("calling the interactive_action tool plays the clip", res.ok and rig.robot.clips(ctrl)[-1:] == ["wave_left"],
              f"result {res}; clips sent: {rig.robot.clips(ctrl)}", passes="return await interactive_action(robot, action)")
    finally:
        rig.close()
    shake = HERE.parent / "tricks" / "sit_stay_shake" / "trick.yaml"
    if shake.exists():
        import yaml

        spec = yaml.safe_load(shake.read_text()) or {}
        text = json.dumps(spec)
        if "TODO" in text:
            skip("TODO 10: tricks/sit_stay_shake/trick.yaml still says TODO")
        else:
            check("sit_stay_shake/trick.yaml describes the trick (at least 20 characters)",
                  len(str(spec.get("description", ""))) >= 20, f"description: {spec.get('description')!r}",
                  passes='description: "Sit and stay sitting. Use it when someone says sit."')
            check("... and each of its actions (at least 10 characters each)",
                  all(len(str(v)) >= 10 for v in (spec.get("actions") or {}).values())
                  and bool(spec.get("actions")), f"actions: {spec.get('actions')}",
                  passes='shake_left: "Shakes the left paw, then keeps sitting." (fails: shake_left: "left")')
    else:
        print("  --   (no tricks/sit_stay_shake yet: fetch your policy with get_trick.py, Part 5)")


# ----------------------------------------------------------------------------- Part 7
async def todo11() -> None:
    print("TODO 11: prompt.md, second pass")
    text = prompt_sections("Tricks", "Safety").lower()
    if "todo" in text or not text:
        skip("TODO 11: prompt.md still says TODO under Tricks or Safety")
        return
    check('the prompt says what to do when someone says stop (says "stop")', "stop" in text,
          passes='"- If someone says stop, call stop right away."')
    check('... and when Pupper falls over (says "fall" or "fell")', "fall" in text or "fell" in text,
          passes='"- If you fall over, ask a person to set you upright, then call stand_up."')
    check('... and how to handle several requests in a row (says "order", "row" or "sequence")',
          "order" in text or "row" in text or "sequence" in text,
          passes='"- If the person asks for several things in a row, call the tools in that order."')


# ----------------------------------------------------------------------------- provided
async def provided() -> None:
    print("The provided robot (these should pass before you change anything)")
    rig = Rig()
    try:
        bad = await tools.call(rig.ts, "stand_up", {"speed": 3})
        check("a tool call with a parameter the tool doesn't have is refused", not bad.ok, f"got {bad}")
        bad = await tools.call(rig.ts, "play_trick", {"name": "backflip"})
        check("a value outside a tool's choices is refused, listing the choices",
              not bad.ok and "bow" in bad.message, f"got {bad}")

        rig.robot.controllers[LOCOMOTION_CONTROLLER] = "inactive"
        rig.ts.walk_since = None
        await behaviors.stand_up(rig.ts)
        on_at = rig.clock.now
        await rig.settle()
        check("stand_up hands the legs to the locomotion policy", LOCOMOTION_CONTROLLER in rig.robot.active())
        check("... and waits until the policy is ready before the next job starts",
              rig.clock.now - on_at >= WALK_READY_S - 0.01, f"ready after {rig.clock.now - on_at:.2f} s")
        await behaviors.lie_down(rig.ts)
        await rig.settle()
        check("lie_down relaxes every leg controller", not (rig.robot.active() & rig.ts.leg_controllers),
              f"active: {sorted(rig.robot.active())}")
        await behaviors.stand_up(rig.ts)
        await rig.settle()

        # A tilt e-stop: the controller says so on /rosout.
        rig.robot.send("/rosout", _Msg(name="gemini_walk", msg=robot_api.ESTOP_LOG))
        await rig.settle()
        res = await tools.call(rig.ts, "robot_status", {})
        check("after a fall, robot_status says it fell over", "fall" in res.message.lower() or "fell" in res.message.lower(),
              f"got {res.message!r}")
        rig.imu(tilt_deg=70)
        res = await rig.ts.switch_to(LOCOMOTION_CONTROLLER)
        check("standing up while still tipped over is refused, asking for help",
              not res.ok and "upright" in res.message, f"got {res}")
        rig.imu(tilt_deg=3)
        res = await rig.ts.switch_to(LOCOMOTION_CONTROLLER)
        check("once upright and still, standing up works again", res.ok and rig.ts.fallen is None, f"got {res}")

        await rig.ts.wait(5.0)
        res = await tools.call(rig.ts, "stop", {})
        check("stop clears the queue", rig.ts.commands.empty() and "stopped" in res.message, f"got {res}")
    finally:
        rig.close()

    # The server restarted while the robot sat in an interactive trick.
    rig = Rig()
    try:
        hold = fixture_tricks()["hold_pose"]
        rig.robot.controllers[LOCOMOTION_CONTROLLER] = "inactive"
        rig.robot.controllers[hold.controller] = "active"
        rig.ts.worker.cancel()
        rig.ts = Robot()  # a fresh server, the robot still sitting
        await rig.settle()
        check("a restarted server notices the robot is still in an interactive trick",
              getattr(rig.ts.state_now, "name", None) == "hold_pose", f"state_now: {rig.ts.state_now}")
        await behaviors.stand_up(rig.ts)
        await rig.settle()
        check("... so standing up plays the trick's exit clip first",
              rig.robot.clips(hold.controller)[-1:] == ["leave"] and LOCOMOTION_CONTROLLER in rig.robot.active(),
              f"clips sent: {rig.robot.clips(hold.controller)}; active: {sorted(rig.robot.active())}")
    finally:
        rig.close()

    # lab/chat.py's fake-robot mode tells Gemini exactly what the web app would.
    import gemini_chat
    desk = gemini_chat.DeskBackend()
    prompt, decls = desk.setup()
    try:
        check("chat.py gives Gemini the same prompt as the robot server", prompt == system_prompt())
        check("... and the same tools", decls == tools.declarations(desk.rig.ts),
              f"chat.py: {[d['name'] for d in decls]}")
    finally:
        desk.rig.close()

    # The stop tool, in the middle of things.
    rig = Rig()
    try:
        bow, hold = rig.ts.tricks["bow"], rig.ts.tricks["hold_pose"]
        await rig.ts.queue("trick bow", lambda: behaviors.play_policy_trick(rig.ts, bow), bow.total_time, then=LOCOMOTION)
        t0 = rig.clock.now
        while rig.clock.now < t0 + 2.0:  # two seconds into the bow
            await _real_sleep(0)
        check("(setup) the bow is playing", bow.controller in rig.robot.active())
        res = await tools.call(rig.ts, "stop", {})
        await rig.settle()
        check("stop in the middle of a trick relaxes the motors (a trick policy plays on by itself)",
              not (rig.robot.active() & rig.ts.leg_controllers), f"active: {sorted(rig.robot.active())}")
        check("... and the reply says so", res.ok and "relaxed" in res.message, f"got {res}")

        async def walking_job():
            await rig.ts.switch_to(LOCOMOTION_CONTROLLER)
            await rig.ts.sleep(5.0)
            return robot_api.Result(True, "walked")

        await rig.ts.queue("walk", walking_job, 5.0, then=LOCOMOTION)
        t0 = rig.clock.now
        while rig.clock.now < t0 + 6.0:  # standing up (4.2 s), then walking
            await _real_sleep(0)
        res = await tools.call(rig.ts, "stop", {})
        await rig.settle()
        check("stop during a walk just stands still: the locomotion policy keeps the legs",
              LOCOMOTION_CONTROLLER in rig.robot.active() and "relaxed" not in res.message, f"got {res}")

        await rig.ts.queue("trick hold_pose", lambda: behaviors.play_policy_trick(rig.ts, hold), hold.total_time,
                           then=hold)
        await rig.settle()
        res = await tools.call(rig.ts, "stop", {})
        await rig.settle()
        check("stop while a trick only holds its pose leaves it holding (nothing is moving)",
              hold.controller in rig.robot.active() and "relaxed" not in res.message, f"got {res}")
    finally:
        rig.close()

    # stop is immediate and clears everything, even requests still being handled.
    rig = Rig()
    try:
        bow = rig.ts.tricks["bow"]

        async def long_walk():
            await rig.ts.switch_to(LOCOMOTION_CONTROLLER)
            while True:
                rig.ts.send_speed(0.4, 0, 0)
                await rig.ts.sleep(0.05)

        await rig.ts.queue("walk", long_walk, 10.0, then=LOCOMOTION)
        await rig.ts.queue("trick bow", lambda: behaviors.play_policy_trick(rig.ts, bow), bow.total_time, then=LOCOMOTION)
        t0 = rig.clock.now
        for _ in range(100000):  # walking for a second
            if rig.clock.now >= t0 + 1.0:
                break
            await _real_sleep(0)
        rig.robot.published.clear()
        n_switches, t_stop = len(rig.robot.switches), rig.clock.now
        res = await tools.call(rig.ts, "stop", {})
        sent = rig.robot.twists()
        check("stop sends a zero speed at once, before anything else",
              bool(sent) and sent[0][0] == t_stop and sent[0][1].linear.x == 0.0,
              f"first speeds sent: {[(round(t - t_stop, 2), m.linear.x) for t, m in sent[:3]]}")
        check("... and empties the queue right away (the running job is cancelled)",
              rig.ts.commands.empty() and (rig.ts.current is None or rig.ts.current.done()),
              f"queued: {rig.ts.commands.qsize()}; running: {rig.ts.current}")
        await rig.settle()
        later = [sw for sw in rig.robot.switches[n_switches:] if bow.controller in str(sw)]
        check("... so nothing that was queued behind runs (the bow never plays)", not later, f"switches: {later}")
        check("... and the walk's speeds stop", all(m.linear.x == 0.0 for _, m in rig.robot.twists()),
              f"speeds after the stop: {sorted({m.linear.x for _, m in rig.robot.twists()})}")

        # follow(true) waits while the legs switch; a stop in that time wins.
        await behaviors.lie_down(rig.ts)
        await rig.settle()
        follow = asyncio.ensure_future(tools.call(rig.ts, "follow", {"enabled": True}))
        t0 = rig.clock.now
        while rig.clock.now < t0 + 1.0 and not follow.done():  # still standing up (4.2 s)
            await _real_sleep(0)
        if follow.done() and unwritten(follow.result()):
            skip(f"the follow-and-stop check needs {unwritten(follow.result())} first")
        else:
            await tools.call(rig.ts, "stop", {})
            await rig.settle()
            res = await follow
            check("a stop while follow mode is starting: it doesn't start", not rig.ts.following and not res.ok,
                  f"following: {rig.ts.following}; follow said: {res}")

        # A request still in its *now* part when the stop came in doesn't queue its job.
        token = robot_api.CALL_STOPS.set(rig.ts.stops)
        try:
            await rig.ts.immediate_stop()
            res = await behaviors.stand_up(rig.ts)
        finally:
            robot_api.CALL_STOPS.reset(token)
        check("a request that began before a stop doesn't queue anything after it",
              not res.ok and rig.ts.commands.empty(), f"got {res}; queued: {rig.ts.commands.qsize()}")
    finally:
        rig.close()

    # A trick that walks in its own stance (the spider): its clips, then its walking policy.
    def creep_tricks() -> dict[str, Trick]:
        tricks = fixture_tricks()
        tricks["creep"] = Trick(
            name="creep", description="Crouches low and creeps.", kind="policy", path=Path("creep.json"),
            duration=8.0, end_pose="crouched",
            clips=[_clip("down", 0, 200, next="idle"), _clip("idle", 199, 250, loop=True), _clip("up", 249, 450)],
            actions={"up": "Stands back up."}, exit_clip="up",
            walk_path=Path("creep_walk.json"),
            walk=WalkConfig(speed=0.1, max_forward=0.15, max_sideways=0.1, turn=30.0, note="It creeps."),
        )
        return tricks

    rig = Rig(tricks=creep_tricks)
    try:
        creep = rig.ts.tricks["creep"]
        res = await behaviors.play_policy_trick(rig.ts, creep)
        check("a walking trick: its clips play, then its walking policy takes the legs",
              creep.walk_controller in rig.robot.active() and creep.controller not in rig.robot.active()
              and rig.robot.start_clips.get(creep.controller) == [""] and rig.ts.state_now is creep,
              f"active: {sorted(rig.robot.active())}; start_clips: {rig.robot.start_clips}; got {res}")
        check("... and the outcome says it walks in its stance", res.ok and "walk" in res.message, f"got {res}")

        rig.ts.state_after_queue = creep  # as play_trick's then= leaves it
        check("robot.speeds() in a walking trick: its own defaults and limits",
              rig.ts.speeds().walk == 0.1 and rig.ts.speeds().turn == 30.0 and rig.ts.speeds().limits.vx_max == 0.15,
              f"got {rig.ts.speeds()}")
        rig.robot.published.clear()
        res = await behaviors.walk(rig.ts, 0.3, 0.0, 0.0, 2.0)
        await rig.settle()
        if unwritten(res):
            skip(f"walking in the stance needs {unwritten(res)} first")
        else:
            check("a move too fast for the trick is refused, with its limit, and nothing is sent",
                  not res.ok and "0.15" in res.message and "creep" in res.message and not rig.robot.twists(),
                  f"got {res}", passes='"too fast: |vx|<=0.15 m/s (in creep)"')
            res = await behaviors.walk(rig.ts, 0.1, 0.0, 0.0, 2.0)
            await rig.settle()
            sent = [m.linear.x for _, m in rig.robot.twists() if m.linear.x]
            check("a move within its limits drives the trick's walking policy, and Pupper stays in the trick",
                  res.ok and creep.walk_controller in rig.robot.active() and sent and abs(max(sent) - 0.1) < 1e-6
                  and abs(len(sent) / LLM_CMD_RATE - 2.0) < 0.1 and rig.ts.state_now is creep,
                  f"got {res}; active: {sorted(rig.robot.active())}; sent {len(sent) / LLM_CMD_RATE:.1f} s")
            rig.robot.published.clear()
            res = await rig.call("turn", direction="left", angle=90)
            await rig.settle()
            turned = [math.degrees(m.angular.z) for _, m in rig.robot.twists() if m.angular.z]
            if unwritten(res):
                skip(f"turning in the stance needs {unwritten(res)} first")
            else:
                check("turn in the trick turns at its speed (robot.speeds().turn), as far as asked",
                      res.ok and turned and abs(max(turned) - 30.0) < 0.1
                      and abs(sum(turned) / LLM_CMD_RATE - 90.0) < 3.0,
                      f"got {res}; top {max(turned or [0]):.1f} deg/s, {sum(turned) / LLM_CMD_RATE:.0f} deg",
                      passes="turn_speed = robot.speeds().turn")

        await behaviors.stand_up(rig.ts)
        await rig.settle()
        check("standing up leaves through the exit clip: the trick's policy, started at it",
              rig.robot.start_clips.get(creep.controller) == ["", "up"]
              and rig.robot.activations(creep.controller)[-1] > rig.robot.deactivations(creep.walk_controller)[-1] - 1e-6
              and LOCOMOTION_CONTROLLER in rig.robot.active() and rig.ts.state_now == LOCOMOTION,
              f"start_clips: {rig.robot.start_clips}; active: {sorted(rig.robot.active())}; state {rig.ts.state_now}")

        await behaviors.play_policy_trick(rig.ts, creep)
        await rig.ts.emergency_stop()
        check("the e-stop relaxes a trick's walking policy too", not (rig.robot.active() & rig.ts.leg_controllers),
              f"active: {sorted(rig.robot.active())}")
    finally:
        rig.close()

    # replaced_by: a trick steps aside while its replacement is installed.
    import tricks as tricks_mod
    with tempfile.TemporaryDirectory() as tmp:
        def add(name, extra=""):
            (Path(tmp) / name).mkdir()
            (Path(tmp) / name / "policy.json").write_text(json.dumps({"trick": {"duration_s": 1.0}}))
            (Path(tmp) / name / "trick.yaml").write_text(f"description: {name}\ntype: policy\npolicy: policy.json\n{extra}")
        add("old_sit", "replaced_by: new_sit\n")
        check("a trick with replaced_by is there while its replacement isn't",
              "old_sit" in tricks_mod.load_tricks(Path(tmp)))
        add("new_sit")
        got = tricks_mod.load_tricks(Path(tmp))
        check("... and steps aside once the replacement is installed", "old_sit" not in got and "new_sit" in got,
              f"tricks: {sorted(got)}")

    def no_clip_tricks():
        return {k: v for k, v in fixture_tricks().items() if not v.clips}

    rig = Rig(no_clip_tricks)
    try:
        decls = {d["name"]: d for d in tools.declarations(rig.ts)}
        check("interactive_action is offered to Gemini even before an interactive trick is installed", "interactive_action" in decls,
              f"declared: {sorted(decls)}")
        res = await tools.call(rig.ts, "interactive_action", {"action": "shake_left"})
        check("... and says nothing is installed yet", not res.ok and "installed" in res.message, f"got {res}")
    finally:
        rig.close()


# ----------------------------------------------------------------------------- the skateboard
_POLICY_CLIP = {"command_clip": {"vx": [0.0, 0.3], "vy": [0.0, 0.0], "wz": [-0.6, 0.6]},
                "heading_hold": {"kp": 1.0, "clip": 0.1, "yaw_threshold": 0.1, "walk_threshold": 0.05}}


_ENTRY_KEYS = ("file", "tested", "travel_m_per_s", "turn_deg_per_s", "coast_m")  # per policy, under policies:


def _write_skate(policy: dict | None = None, **changes) -> Path:
    """A skate folder in a fresh temp dir: skate.yaml (with changes; None leaves a key
    out) and a policy stub (``policy`` instead of the default one). The yaml selects
    its one catalogue entry, steer_v6; changes to an entry's keys (file, tested and
    the sim estimates) go into that entry, the rest to the top level."""
    entry = {"file": "policy.json", "tested": "robot 2026-11-26: a test entry", "travel_m_per_s": 0.3,
             "turn_deg_per_s": {"gentle": 0.7, "medium": 1.0, "hard": 1.9}, "coast_m": 0.3}
    spec = {"policy": "steer_v6", "speed": 0.3, "carve": {"gentle": 0.15, "medium": 0.3, "hard": 0.5},
            "default_seconds": 3, "max_seconds": 5, "queue_max_seconds": 10}
    for k, v in changes.items():
        (entry if k in _ENTRY_KEYS else spec)[k] = v
    entry = {k: v for k, v in entry.items() if v is not None}
    spec = {k: v for k, v in {**spec, "policies": {"steer_v6": entry}}.items() if v is not None}
    return _write_spec(spec, policy)


def _write_spec(spec: dict, policy: dict | list | str | None = None) -> Path:
    """A temp skate folder with exactly this skate.yaml, and policy.json (a str is written as is)."""
    d = Path(tempfile.mkdtemp(prefix="gemini_pupper_skate_"))
    (d / "skate.yaml").write_text(yaml.safe_dump(spec))
    body = _POLICY_CLIP if policy is None else policy
    (d / "policy.json").write_text(body if isinstance(body, str) else json.dumps(body))
    return d


def _load_error(d: Path) -> str:
    """load_skate's SkateConfigError for a folder ("" if it loads; any other error, by name)."""
    try:
        load_skate(d)
    except skateboard.SkateConfigError as e:
        return str(e)
    except Exception as e:
        return f"not a SkateConfigError: {type(e).__name__}: {e}"
    return ""


def _skate_error(policy: dict | None = None, **changes) -> str:
    return _load_error(_write_skate(policy, **changes))


async def skate_config() -> None:
    print("Skate: skate.yaml and the policy next to it")
    check("a folder with no skate.yaml means no skate policy is installed",
          load_skate(Path(tempfile.mkdtemp(prefix="gemini_pupper_noskate_"))) is None)
    cfg = load_skate(_write_skate())
    check("a good skate.yaml loads", cfg is not None and cfg.speed == 0.3 and cfg.carve["hard"] == 0.5
          and cfg.max_seconds == 5.0 and cfg.policy_path.name == "policy.json", f"got {cfg}")
    for what, changes, word in [
        ("a speed above the policy's clip", {"speed": 0.4}, "0.3"),
        ("a carve above the policy's clip", {"carve": {"gentle": 0.15, "medium": 0.3, "hard": 0.7}}, "0.6"),
        ("a carve at the heading hold's threshold", {"carve": {"gentle": 0.1, "medium": 0.3, "hard": 0.5}}, "0.14"),
        ("carves that do not get sharper", {"carve": {"gentle": 0.3, "medium": 0.2, "hard": 0.5}}, "sharper"),
        ("a missing sharpness", {"carve": {"gentle": 0.15, "hard": 0.5}}, "medium"),
        ("a command longer than the queue", {"max_seconds": 12}, "queue_max_seconds"),
        ("a queue longer than 10 s", {"queue_max_seconds": 20}, "10"),
        ("a default longer than the longest command", {"default_seconds": 6}, "max_seconds"),
    ]:
        err = _skate_error(**changes)
        check(f"{what} is refused, with the number", bool(err) and word in err, f"error: {err!r}")
    wide = {"command_clip": {"vx": [0.0, 0.5], "vy": [0.0, 0.0], "wz": [-0.8, 0.8]}}
    for what, policy, changes, word in [
        ("a speed above the 0.3 m/s ceiling, even if the policy's clip allows it", wide, {"speed": 0.4}, "0.3"),
        ("a carve above the 0.5 rad/s ceiling, even if the policy's clip allows it", wide,
         {"carve": {"gentle": 0.15, "medium": 0.3, "hard": 0.6}}, "0.5"),
        ("a command longer than the 5 s ceiling", None, {"max_seconds": 6}, "5"),
        ("a missing key, named", None, {"coast_m": None}, "coast_m"),
        ("a missing turn estimate, named", None, {"turn_deg_per_s": {"gentle": 1.0, "medium": 1.4}}, "hard"),
        ("a speed that is not a number, named", None, {"speed": "fast"}, "speed"),
        ("a travel estimate that is not positive", None, {"travel_m_per_s": 0}, "travel_m_per_s"),
        ("a turn estimate that is not positive", None, {"turn_deg_per_s": {"gentle": -1, "medium": 1.4, "hard": 2.2}},
         "turn_deg_per_s"),
        ("a coast estimate that is not positive", None, {"coast_m": 0}, "coast_m"),
        ("a policy without a command_clip, named", {"heading_hold": {}}, {}, "command_clip"),
    ]:
        err = _skate_error(policy, **changes)
        check(f"{what} is refused as a ValueError, with the file", bool(err) and word in err and "skate.yaml" in err
              and "not a SkateConfigError" not in err, f"error: {err!r}")
    d = _write_skate()
    (d / "skate.yaml").write_text("")
    err = _load_error(d)
    check("an empty skate.yaml is refused as a ValueError, with the file", "skate.yaml" in err
          and "not a SkateConfigError" not in err, f"error: {err!r}")
    d = _write_skate()
    (d / "policy.json").unlink()
    try:
        load_skate(d)
        err = ""
    except ValueError as e:
        err = str(e)
    check("a missing policy file is refused, and says where it goes", "policy.json" in err, f"error: {err!r}")
    text = cfg.describe()
    check("describe() gives Gemini the numbers", all(w in text for w in ("0.3 m/s", "5 s", "10 s", "1.9")), text)
    check("... and says they are estimates and that Pupper cannot measure distance",
          "estimate" in text and "cannot measure distance" in text, text)
    base = {"model_path": "walk.json", "chord_partner": "x", "game_buttons": True, "max_body_angle": 1.0, "kp_scale": [1.0]}
    params = skate_controller_params(cfg, base)
    check("the skate controller runs the skate policy, with no joystick game features",
          params["model_path"] == str(cfg.policy_path) and params["game_buttons"] is False
          and params["reference_tricks"] is False and "chord_partner" not in params, f"got {params}")
    check("... and keeps the course controller's tilt limit and trims",
          params["max_body_angle"] == 1.0 and params["kp_scale"] == [1.0] and base["game_buttons"] is True)
    real = load_skate()
    check("the repo's own skate/skate.yaml loads", real is not None and real.speed <= 0.3
          and real.carve["hard"] <= 0.5 and real.max_seconds <= 5 and real.queue_max_seconds <= 10, f"got {real}")
    check("the on-board tools are the skate tools, stop and robot_status",
          skateboard.ON_BOARD_TOOLS == {"start_skating", "skate_forward", "skate_turn", "end_skating", "stop", "robot_status"})


def _safe(d: Path):
    """load_skate_safe for a folder, or ("raised", the error) if it raised."""
    try:
        return skateboard.load_skate_safe(d)
    except Exception as e:
        return "raised", f"{type(e).__name__}: {e}", None


async def skate_catalogue() -> None:
    print("Skate: the policy catalogue in skate.yaml, and a bad config turns skate mode off")
    real = load_skate()
    check("the repo's skate/skate.yaml selects steer_v6", real is not None and real.name == "steer_v6", f"got {real}")
    check("... its policy file is policies/split_steer_v6_max0.3_policy.json, and it is there", real is not None
          and real.policy_path.as_posix().endswith("policies/split_steer_v6_max0.3_policy.json")
          and real.policy_path.exists(), f"got {real and real.policy_path}")
    check("... and it says what it has done on the robot", real is not None and real.tested.startswith("robot 2026-11-26"),
          f"got {real and real.tested!r}")

    spec = yaml.safe_load((skateboard.SKATE_DIR / "skate.yaml").read_text())
    for name in spec["policies"]:
        d = Path(tempfile.mkdtemp(prefix="gemini_pupper_skate_"))
        (d / "skate.yaml").write_text(yaml.safe_dump({**spec, "policy": name}))
        (d / "policies").symlink_to(skateboard.SKATE_DIR / "policies")
        err = _load_error(d)
        cfg = None if err else load_skate(d)
        entry = spec["policies"][name]
        check(f"the catalogue entry {name} loads when selected, and passes every check", not err and cfg is not None
              and cfg.name == name and cfg.policy_path.exists() and cfg.policy_path.name == Path(entry["file"]).name
              and cfg.speed <= skateboard.MAX_SPEED and cfg.carve["hard"] <= skateboard.MAX_CARVE
              and cfg.coast_m == float(entry["coast_m"]), f"error: {err!r}; got {cfg}")

    good = yaml.safe_load((_write_skate() / "skate.yaml").read_text())
    err = _load_error(_write_spec({**good, "policy": "steer_v99"}))
    check("a policy name that is not in the catalogue is refused, with the file, the name and the real choices",
          "skate.yaml" in err and "steer_v99" in err and "steer_v6" in err and "not a SkateConfigError" not in err,
          f"error: {err!r}")

    entry = good["policies"]["steer_v6"]
    old = {k: v for k, v in good.items() if k != "policies"}
    old.update(policy="policy.json", travel_m_per_s=0.3, turn_deg_per_s=entry["turn_deg_per_s"], coast_m=0.3)
    bad_cases = [
        ("a skate.yaml in the old single-policy layout", _write_spec(old), "old single-policy layout"),
        ("a missing policies map", _write_spec({k: v for k, v in good.items() if k != "policies"}), "policies"),
        ("policies that is not a map", _write_spec({**good, "policies": ["steer_v6"]}), "policies"),
        ("a catalogue entry that is not a map", _write_spec({**good, "policies": {"steer_v6": "policy.json"}}), "steer_v6"),
        ("an entry without a file", _write_skate(file=None), "file"),
        ("an entry without tested", _write_skate(tested=None), "tested"),
        ("a policy file that is not there", _write_skate(file="gone.json"), "gone.json"),
        ("a policy file that is not JSON", _write_skate(policy="{not json"), "policy.json"),
        ("a policy JSON that is a list", _write_skate(policy=[1, 2]), "policy.json"),
        ("a heading_hold that is not a mapping", _write_skate(policy={**_POLICY_CLIP, "heading_hold": [0.1]}),
         "heading_hold"),
        ("carve: 0.3, not a mapping", _write_skate(carve=0.3), "carve"),
        ("turn_deg_per_s: 2, not a mapping", _write_skate(turn_deg_per_s=2), "turn_deg_per_s"),
        ("a policy name that is not text", _write_spec({**good, "policy": ["steer_v6"]}), "policy"),
        ("a skate.yaml that is not YAML", None, "skate.yaml"),
    ]
    for i, (what, d, word) in enumerate(bad_cases):
        if d is None:
            d = _write_skate()
            (d / "skate.yaml").write_text("policy: [unclosed\n")
            bad_cases[i] = (what, d, word)
        err = _load_error(d)
        check(f"{what} is a SkateConfigError naming the file and the key", bool(err) and "skate.yaml" in err
              and word in err and "not a SkateConfigError" not in err, f"error: {err!r}")

    empty = Path(tempfile.mkdtemp(prefix="gemini_pupper_noskate_"))
    check("load_skate_safe: no skate.yaml is (None, None, None), skate mode not installed",
          _safe(empty) == (None, None, None), f"got {_safe(empty)}")
    got = _safe(_write_skate())
    check("load_skate_safe: a good skate.yaml is (config, None, None)", isinstance(got[0], SkateConfig)
          and got[1:] == (None, None), f"got {got}")
    for what, d, _ in bad_cases + [("a policy name not in the catalogue", _write_spec({**good, "policy": "steer_v99"}), "")]:
        got = _safe(d)
        check(f"load_skate_safe: {what} is (None, the message, the detail for the log), and nothing raised",
              got[0] is None and isinstance(got[1], str) and got[1].startswith("skate.yaml: ")
              and isinstance(got[2], str) and str(d) in got[2], f"got {got}")

    check("not_installed_message(None) is the not-installed text",
          skateboard.not_installed_message(None) == skateboard.NOT_INSTALLED, skateboard.not_installed_message(None))
    msg = skateboard.not_installed_message("skate.yaml: policy 'x' is not one of steer_v6")
    check("not_installed_message(error) says skate mode is off because of the config, and quotes the error",
          msg == "skate mode is off on this robot because its config has an error (skate.yaml: policy 'x' is not one"
                 " of steer_v6), so Pupper can't skate", msg)

    # Fix round 1: what a bad config looks like, and the cases the first loader let through.
    for what, d, _ in bad_cases:
        err = _load_error(d)
        check(f"{what}: the error starts with the bare file name and has no directory or exception text",
              err.startswith("skate.yaml: ") and str(d) not in err and "Error" not in err and "line " not in err,
              f"error: {err!r}")

    def raw(text: str) -> Path:
        d = _write_skate()
        (d / "skate.yaml").write_text(text)
        return d

    text = (_write_skate() / "skate.yaml").read_text()
    dup_entry = text.replace("policies:\n", "policies:\n  steer_v6:\n    file: other.json\n", 1)
    for what, d, word in [
        ("a catalogue entry written twice", raw(dup_entry), "steer_v6"),
        ("a top-level policy: line written twice", raw("policy: steer_v99\n" + text), "policy"),
        ("a nested key written twice", raw(text.replace("  gentle: 0.15\n", "  gentle: 0.15\n  gentle: 0.2\n", 1)),
         "gentle"),
    ]:
        err = _load_error(d)
        check(f"{what} is a SkateConfigError naming the key", err.startswith("skate.yaml: ") and word in err
              and "twice" in err and "not a SkateConfigError" not in err, f"error: {err!r}")
    outside = Path(tempfile.mkdtemp(prefix="gemini_pupper_outside_")) / "policy.json"
    outside.write_text(json.dumps(_POLICY_CLIP))
    sub = _write_skate()
    (sub / "policies").mkdir()
    for what, d, word in [
        ("a policy file given as an absolute path", _write_skate(file=str(outside)), "skate folder"),
        ("a policy file outside the skate folder (..)", _write_skate(file="../policy.json"), "skate folder"),
        ("a policy file that is a folder", _write_skate(file="."), "not a file"),
        ("a heading_hold of 0", _write_skate(policy={**_POLICY_CLIP, "heading_hold": 0}), "heading_hold"),
        ("a heading_hold of []", _write_skate(policy={**_POLICY_CLIP, "heading_hold": []}), "heading_hold"),
        ('a heading_hold of ""', _write_skate(policy={**_POLICY_CLIP, "heading_hold": ""}), "heading_hold"),
        ("a heading_hold of false", _write_skate(policy={**_POLICY_CLIP, "heading_hold": False}), "heading_hold"),
        ("coast_m: .inf", raw(text.replace("coast_m: 0.3", "coast_m: .inf")), "coast_m"),
        ("travel_m_per_s: true", _write_skate(travel_m_per_s=True), "travel_m_per_s"),
        ("speed: .nan", raw(text.replace("speed: 0.3", "speed: .nan")), "speed"),
        ("a skate.yaml nested thousands deep", raw("policy: " + "[" * 20000 + "]" * 20000 + "\n"), "YAML"),
        ("a policy JSON nested thousands deep", _write_skate(policy="[" * 20000 + "]" * 20000), "JSON"),
    ]:
        err = _load_error(d)
        check(f"{what} is a SkateConfigError naming the key", err.startswith("skate.yaml: ") and word in err
              and "not a SkateConfigError" not in err and "Error" not in err, f"error: {err!r}")
    within = _write_skate(file="policies/policy.json")
    (within / "policies").mkdir()
    (within / "policies" / "policy.json").write_text(json.dumps(_POLICY_CLIP))
    check("a policy file in a subfolder of the skate folder still loads", _load_error(within) == "",
          _load_error(within))
    check("a heading_hold of null means no hold, and loads",
          _load_error(_write_skate(policy={**_POLICY_CLIP, "heading_hold": None})) == "")

    real_load = skateboard.load_skate
    skateboard.load_skate = lambda skate_dir=skateboard.SKATE_DIR: [][0]  # a bug in the loader: IndexError
    try:
        unexpected = _safe(_write_skate())
    finally:
        skateboard.load_skate = real_load
    check("an unexpected exception in the loader: (None, 'skate.yaml could not be loaded', the detail)",
          unexpected[0] is None and unexpected[1] == "skate.yaml could not be loaded"
          and "IndexError" in (unexpected[2] or ""), f"got {unexpected}")
    spoken_cases = [
        ("a policy file that is not JSON", _safe(_write_skate(policy="{not json"))),
        ("a duplicate key", _safe(raw(dup_entry))),
        ("a missing key", _safe(_write_skate(coast_m=None))),
        ("an unexpected exception", unexpected),
    ]
    for what, (_, error, detail) in spoken_cases:
        rig = Rig(skate_error=error, skate_detail=detail)
        try:
            said = skateboard.not_installed_message(rig.ts.skate_error)
            desc = {d["name"]: d for d in tools.declarations(rig.ts)}["skate_forward"]["description"]
            placeholder = desc[desc.index("("):desc.rindex(")") + 1] if "(" in desc else desc
            check(f"{what}: what Gemini hears is short and plain (no /, no Error, no Traceback, under 200 characters)",
                  all("/" not in t and "Error" not in t and "Traceback" not in t and len(t) < 200
                      for t in (said, placeholder)), f"said {said!r}; {{skate}} -> {placeholder!r}")
            res = await rig.call("start_skating", placed=True)
            check(f"... start_skating says it word for word", not res.ok and res.message == said, f"got {res}")
            check("... and the log gets the full detail", any(r.levelno >= logging.ERROR and detail in r.getMessage()
                                                           for r in rig.records), [r.getMessage() for r in rig.records])
        finally:
            rig.close()

    error = _safe(_write_spec({**good, "policy": "steer_v99"}))[1]
    rig = Rig(skate_error=error)
    try:
        check("a robot with a broken skate config starts, with skate mode off and the error kept",
              rig.ts.skate is None and rig.ts.skate_error == error and SKATE_CONTROLLER not in rig.ts.leg_controllers,
              f"skate {rig.ts.skate}; skate_error {getattr(rig.ts, 'skate_error', 'missing')!r}")
        check("... and it logs the error", any(r.levelno >= logging.ERROR and error in r.getMessage()
                                               for r in rig.records), [r.getMessage() for r in rig.records][-5:])
        decls = {d["name"]: d for d in tools.declarations(rig.ts)}
        text = decls.get("skate_forward", {}).get("description", "")
        check("... the skate tools' descriptions say why skate mode is off", "config has an error" in text
              and "{skate}" not in text, text)
        await rig.call("lie_down")
        res = await rig.call("start_skating", placed=True)
        check("... start_skating answers with the config error", not res.ok
              and res.message == skateboard.not_installed_message(error), f"got {res}")
        res = await rig.call("skate_forward")
        check("... and so does skate_forward", not res.ok and res.message == skateboard.not_installed_message(error),
              f"got {res}")
        res = await rig.call("stand_up")
        check("... while stand_up still works", res.ok and rig.ts.state_now == LOCOMOTION, f"got {res}")
        start = len(rig.robot.twists())
        res = await rig.call("move", direction="forward", duration=1.0)
        if unwritten(res):
            skip(f"{unwritten(res)} is not written yet (the move tool)")
        else:
            check("... and so does walking (move)", res.ok and walk_twists_since(rig, start), f"got {res}")
    finally:
        rig.close()


def fixture_skate() -> SkateConfig:
    return SkateConfig(name="steer_v6", tested="robot 2026-11-26: a test entry", policy_path=Path("skate.json"),
                       speed=0.3, carve={"gentle": 0.15, "medium": 0.3, "hard": 0.5}, travel_m_per_s=0.3, turn_deg_per_s={"gentle": 0.7, "medium": 1.0, "hard": 1.9}, coast_m=0.3,
                       default_seconds=3.0, max_seconds=5.0, queue_max_seconds=10.0)


async def skate_robot() -> None:
    print("Skate: the robot's skate mode")
    rig = Rig()
    try:
        check("with no skate policy installed, the robot has no skate controller",
              rig.ts.skate is None and SKATE_CONTROLLER not in rig.ts.leg_controllers)
    finally:
        rig.close()
    rig = Rig(skate=fixture_skate())
    try:
        check("with one installed, the skate controller is a leg controller",
              rig.ts.skate is not None and SKATE_CONTROLLER in rig.ts.leg_controllers)
        await rig.call("lie_down")
        res = await rig.ts.switch_to(SKATE_CONTROLLER)
        check("switching to the skate controller works from motors idle", res.ok, f"got {res}")
        check("... it alone holds the legs", rig.robot.active() & rig.ts.leg_controllers == {SKATE_CONTROLLER},
              f"active: {rig.robot.active()}")
        check("... and the state is skating", rig.ts.state_now == SKATING, f"state: {rig.ts.state_now}")
        res = await rig.ts.mode()
        check("robot_status says it is skating", "skating" in res.message, res.message)
        want = ("mode: skating (policy steer_v6): on the skateboard in skate mode (skate_forward and skate_turn ride it;"
                " end_skating relaxes the motors to get off)")
        check("... and names the loaded policy", res.message == want, res.message, passes=want)
        cfg, rig.ts.skate = rig.ts.skate, None  # the controller left active, but this server's skate.yaml did not load
        try:
            res = await rig.ts.mode()
            check("... with no skate config loaded, it still says skating, without a policy",
                  res.ok and "skating" in res.message and "(policy" not in res.message, res.message)
        except Exception as e:  # noqa: BLE001
            check("... with no skate config loaded, it still says skating, without a policy", False, f"raised {e!r}")
        finally:
            rig.ts.skate = cfg
        res = await rig.ts.switch_to(LOCOMOTION_CONTROLLER)
        check("on the board, the walking policy is refused", not res.ok and "skateboard" in res.message, f"got {res}")
        check("... and nothing switched", rig.robot.active() & rig.ts.leg_controllers == {SKATE_CONTROLLER}
              and rig.ts.state_now == SKATING, f"active: {rig.robot.active()}")
        bow = rig.ts.tricks["bow"]
        res = await rig.ts.switch_to(bow.controller)
        check("... a trick is refused too", not res.ok and "skateboard" in res.message, f"got {res}")
        before = rig.ts.state_after_queue
        res = await rig.ts.set_following(True)
        check("on the board, follow mode is refused", not res.ok and "skateboard" in res.message, f"got {res}")
        check("... the follower stays off and the state is unchanged",
              not rig.robot.follower_on and not rig.ts.following and rig.ts.state_after_queue == before)
        res = await rig.ts.switch_to(None)
        check("relaxing the motors leaves skate mode", res.ok and rig.ts.state_now == robot_api.MOTORS_IDLE
              and not rig.robot.active() & rig.ts.leg_controllers, f"got {res}; state {rig.ts.state_now}")
        res = await rig.ts.switch_to(LOCOMOTION_CONTROLLER)
        check("after that, standing up to walk works again", res.ok and rig.ts.state_now == LOCOMOTION, f"got {res}")

        # Falling off the board: the skate policy's tilt e-stop. Pupper may be resting
        # upright on the deck, so the board guard must hold until a person relaxes it.
        async def board_fall() -> None:
            await rig.ts.switch_to(None)
            rig.imu(tilt_deg=3)
            await rig.ts.switch_to(SKATE_CONTROLLER)
            rig.robot.send("/rosout", _Msg(name=SKATE_CONTROLLER, msg=robot_api.ESTOP_LOG))
            await rig.settle()
            rig.imu(tilt_deg=3)  # upright and still, as if caught on the deck

        await board_fall()
        check("a tilt e-stop in the skate policy is a fall", rig.ts.fallen == SKATE_CONTROLLER
              and rig.ts.state_now == robot_api.FALLEN, f"fallen {rig.ts.fallen}; state {rig.ts.state_now}")
        legs = rig.robot.active() & rig.ts.leg_controllers
        res = await rig.ts.switch_to(LOCOMOTION_CONTROLLER)
        check("after a fall from the board, the walking policy is still refused",
              not res.ok and "skateboard" in res.message, f"got {res}")
        check("... and the legs are untouched", rig.robot.active() & rig.ts.leg_controllers == legs
              and rig.ts.fallen == SKATE_CONTROLLER, f"active: {rig.robot.active()}; fallen {rig.ts.fallen}")
        res = await rig.ts.switch_to(bow.controller)
        check("... a trick too", not res.ok and "skateboard" in res.message, f"got {res}")
        res = await rig.ts.set_following(True)
        check("... and follow mode", not res.ok and "skateboard" in res.message and not rig.robot.follower_on,
              f"got {res}")
        res = await rig.ts.switch_to(SKATE_CONTROLLER)
        check("the skate policy can take the legs back after the fall", res.ok and rig.ts.state_now == SKATING
              and rig.ts.fallen is None, f"got {res}; state {rig.ts.state_now}")
        rig.robot.send("/rosout", _Msg(name=SKATE_CONTROLLER, msg=robot_api.ESTOP_LOG))
        await rig.settle()
        res = await rig.ts.switch_to(None)
        check("relaxing the motors after a fall from the board works", res.ok and rig.ts.state_now == robot_api.MOTORS_IDLE
              and not rig.robot.active() & rig.ts.leg_controllers, f"got {res}; state {rig.ts.state_now}")
        rig.imu(tilt_deg=3)
        res = await rig.ts.switch_to(LOCOMOTION_CONTROLLER)
        check("... and after it, standing up to walk works again", res.ok and rig.ts.state_now == LOCOMOTION, f"got {res}")

        await board_fall()
        rig.robot.send("/emergency_stop", _Msg())
        await rig.settle()
        rig.imu(tilt_deg=3)
        res = await rig.ts.switch_to(LOCOMOTION_CONTROLLER)
        check("the gamepad e-stop after a fall from the board keeps the guard: walking is refused",
              not res.ok and "skateboard" in res.message and rig.ts.fell_off_board
              and not rig.robot.active() & rig.ts.leg_controllers, f"got {res}")
        res = await rig.ts.switch_to(None)
        check("... relaxing the motors (end_skating, with Pupper held) releases it",
              res.ok and not rig.ts.fell_off_board, f"got {res}")
        res = await rig.ts.switch_to(LOCOMOTION_CONTROLLER)
        check("... and then standing up to walk works", res.ok and rig.ts.state_now == LOCOMOTION, f"got {res}")

        await rig.ts.wait(3.0)  # past the e-stop's 2 s window, so a tilt e-stop counts again
        rig.robot.send("/rosout", _Msg(name=LOCOMOTION_CONTROLLER, msg=robot_api.ESTOP_LOG))
        await rig.settle()
        rig.imu(tilt_deg=3)
        res = await rig.ts.switch_to(bow.controller)
        check("a fall while walking is not a board fall: a trick may take the legs", res.ok
              and rig.ts.fallen is None, f"got {res}")
    finally:
        rig.close()


async def _on_board(rig: Rig) -> None:
    """Relax, then start skate mode the way a person and Gemini would."""
    await rig.call("lie_down")
    res = await rig.call("start_skating", placed=True)
    assert res.ok and rig.ts.state_now == SKATING, f"could not start skating: {res}; state {rig.ts.state_now}"


def _ride_twists(rig: Rig, start: int = 0):
    return [(t, m) for t, m in rig.robot.twists()[start:] if abs(m.linear.x) + abs(m.angular.z) > 1e-9]


async def skate_tools() -> None:
    print("Skate: the tools Gemini calls")
    names = ["start_skating", "skate_forward", "skate_turn", "end_skating"]
    rig = Rig()
    try:
        described(rig, names, "skate")
        res = await rig.call("start_skating", placed=True)
        check("with no skate policy installed, start_skating says so", not res.ok and "installed" in res.message, f"got {res}")
        res = await rig.call("skate_forward")
        check("... and so does skate_forward", not res.ok and "installed" in res.message, f"got {res}")
    finally:
        rig.close()

    placement = ("Pupper beside the board on the board's right, body parallel to it and facing the end with the castle"
                 " picture, left feet on the deck's middle line, one ahead of the middle and one behind, right feet on"
                 " the floor, a hand on the harness")
    got = behaviors.PLACEMENT
    check("PLACEMENT says where Pupper stands on the board", got == placement, got, passes=placement)

    rig = Rig(skate=fixture_skate())
    try:
        decls = {d["name"]: d for d in tools.declarations(rig.ts)}
        start_text = decls["start_skating"]["description"]
        check("start_skating's description gives the wait from WALK_READY_S",
              f"about {WALK_READY_S:.0f} s" in start_text, start_text)
        other = behaviors.start_skating_description(7.0)
        check("... and the number follows the wait (7 s gives about 7 s)", "about 7 s" in other and "about 4 s" not in other,
              other)
        stop_text = decls["stop"]["description"]
        check("stop's description says that on the skateboard it stops the pushing and the board rolls on",
              "skateboard" in stop_text and "pushing" in stop_text and "rolls on" in stop_text, stop_text)
        check("robot_status's description lists skating among the states",
              "skating" in decls["robot_status"]["description"], decls["robot_status"]["description"])
        check("skate_forward's description carries the numbers from skate.yaml",
              "0.3 m/s" in decls["skate_forward"]["description"] and "{skate}" not in decls["skate_forward"]["description"],
              decls["skate_forward"]["description"])
        res = await rig.call("skate_forward")
        check("riding before skate mode is refused, and says how to start",
              not res.ok and "start_skating" in res.message and not _ride_twists(rig), f"got {res}")
        res = await rig.call("start_skating", placed=True)
        check("start_skating while standing is refused, and says to lie down first",
              not res.ok and "lie_down" in res.message and not rig.robot.activations(SKATE_CONTROLLER), f"got {res}")
        await rig.call("lie_down")
        res = await rig.call("start_skating", placed=False)
        check("start_skating without a person placing Pupper is refused",
              not res.ok and "place" in res.message and not rig.robot.activations(SKATE_CONTROLLER), f"got {res}")
        check("... and says where to place it", placement in res.message, res.message)
        res = await rig.call("start_skating", placed=True)
        check("start_skating after placing is accepted", res.ok, f"got {res}")
        check("... the skate policy takes the legs", rig.robot.activations(SKATE_CONTROLLER) and rig.ts.state_now == SKATING,
              f"state {rig.ts.state_now}")
        check("... with no failure in the queue", not rig.failures(), f"{rig.failures()}")
        started = rig.robot.activations(SKATE_CONTROLLER)[0]

        mark = len(rig.robot.twists())
        res = await rig.call("skate_forward", seconds=3)
        ride = _ride_twists(rig, mark)
        check("skate_forward is accepted", res.ok, f"got {res}")
        check("... it waits until the policy has faded in", bool(ride) and ride[0][0] >= started + WALK_READY_S - 1e-6,
              f"first command {ride[0][0] - started:.2f} s after the switch" if ride else "no commands")
        check("... sends 0.3 m/s straight ahead, nothing sideways, no turn",
              bool(ride) and all(abs(m.linear.x - 0.3) < 1e-9 and m.linear.y == 0 and m.angular.z == 0 for _, m in ride))
        check("... for 3 s at 20 Hz", 58 <= len(ride) <= 62, f"{len(ride)} commands")
        last = rig.robot.twists()[-1][1]
        check("... and ends with a zero command", last.linear.x == 0 and last.angular.z == 0)
        check("the reply gives the estimate (about 0.9 m) and says it is one",
              "0.9 m" in res.message and "stimate" in res.message and "obstacle" in res.message, res.message)
        check("... and Pupper is still in skate mode", rig.ts.state_now == SKATING and rig.ts.state_after_queue == SKATING)

        for direction, sharpness, wz in [("left", "hard", 0.5), ("right", "hard", -0.5), ("left", "gentle", 0.15)]:
            mark = len(rig.robot.twists())
            res = await rig.call("skate_turn", direction=direction, sharpness=sharpness, seconds=2)
            ride = _ride_twists(rig, mark)
            check(f"skate_turn {direction} {sharpness} carves at {wz:+g} rad/s while riding at 0.3 m/s",
                  res.ok and bool(ride) and all(abs(m.angular.z - wz) < 1e-3 and abs(m.linear.x - 0.3) < 1e-9 for _, m in ride),
                  f"got {res}; z = {sorted({round(m.angular.z, 3) for _, m in ride})}")
        mark = len(rig.robot.twists())
        res = await rig.call("skate_turn", direction="left")
        ride = _ride_twists(rig, mark)
        check("skate_turn defaults to a medium carve for 3 s",
              res.ok and 58 <= len(ride) <= 62 and all(abs(m.angular.z - 0.3) < 1e-3 for _, m in ride), f"got {res}")
        check("... and its reply estimates the turn in degrees", "degrees" in res.message, res.message)

        mark = len(rig.robot.twists())
        res = await rig.call("skate_forward", seconds=6)
        check("a command over 5 s is refused, with the limit", not res.ok and "5 s" in res.message and not _ride_twists(rig, mark),
              f"got {res}")
        res = await rig.call("skate_forward", seconds=0.5)
        check("a command under 1 s is refused, with the limit", not res.ok and "1 s" in res.message, f"got {res}")

        rig.clock.now = max(rig.clock.now, rig.ts.busy_until)  # a job that finishes faster than its estimate leaves a lead in busy_until
        first = await tools.call(rig.ts, "skate_forward", {"seconds": 5})
        second = await tools.call(rig.ts, "skate_forward", {"seconds": 5})
        third = await tools.call(rig.ts, "skate_forward", {"seconds": 1})
        check("10 s of riding can be queued", first.ok and second.ok, f"got {first}; {second}")
        check("... and more than that is refused, with the limit", not third.ok and "10 s" in third.message, f"got {third}")
        await rig.settle()

        switches = len(rig.robot.switches)
        for name, args in [("stand_up", {}), ("lie_down", {}), ("move", {}), ("turn", {}), ("arc", {}),
                           ("follow", {"enabled": True}), ("play_trick", {"name": "bow"})]:
            res = await rig.call(name, **args)
            check(f"{name} is refused on the board, and says why", not res.ok and "skateboard" in res.message, f"got {res}")
        check("... and none of them touched the controllers or the follower",
              len(rig.robot.switches) == switches and not rig.robot.follower_on and rig.ts.state_now == SKATING)

        mark = len(rig.robot.twists())
        await tools.call(rig.ts, "skate_forward", {"seconds": 5})
        for _ in range(40):
            await _real_sleep(0)
        res = await rig.call("stop")
        ride = _ride_twists(rig, mark)
        last = rig.robot.twists()[-1][1]
        check("stop cuts a ride short", res.ok and 0 < len(ride) < 90, f"got {res}; {len(ride)} commands")
        check("... the last command is zero", last.linear.x == 0 and last.angular.z == 0)
        check("... and Pupper stays in skate mode, balancing", rig.ts.state_now == SKATING
              and SKATE_CONTROLLER in rig.robot.active())

        rig.ts.fallen = SKATE_CONTROLLER
        res = await rig.call("skate_forward")
        check("after a fall, riding is refused and says so", not res.ok and "fell" in res.message, f"got {res}")
        rig.ts.fallen = None

        res = await rig.call("end_skating", held=False)
        check("end_skating without a person holding Pupper is refused",
              not res.ok and "hold" in res.message and rig.ts.state_now == SKATING, f"got {res}")
        res = await rig.call("end_skating", held=True)
        check("end_skating with a person holding relaxes the motors",
              res.ok and rig.ts.state_now == robot_api.MOTORS_IDLE and not rig.robot.active() & rig.ts.leg_controllers,
              f"got {res}; state {rig.ts.state_now}")
        res = await rig.call("stand_up")
        check("after that, stand_up works again", res.ok and rig.ts.state_now == LOCOMOTION, f"got {res}")

        # Falling off the board (the skate policy's tilt e-stop, as in skate_robot): the
        # tools keep the on-board rules until end_skating relaxes the motors.
        await _on_board(rig)
        rig.robot.send("/rosout", _Msg(name=SKATE_CONTROLLER, msg=robot_api.ESTOP_LOG))
        await rig.settle()
        rig.imu(tilt_deg=3)  # upright and still, as if caught on the deck
        switches = len(rig.robot.switches)
        res = await rig.call("move")
        check("after a fall from the board, move is refused up front, and says why",
              rig.ts.fell_off_board and not res.ok and "skateboard" in res.message and len(rig.robot.switches) == switches,
              f"got {res}; fell_off_board {rig.ts.fell_off_board}")
        res = await rig.call("end_skating", held=False)
        check("... end_skating without a person holding Pupper is still refused",
              not res.ok and "hold" in res.message and rig.ts.fell_off_board and len(rig.robot.switches) == switches,
              f"got {res}")
        res = await rig.call("end_skating", held=True)
        check("... end_skating with a person holding still relaxes the motors",
              res.ok and rig.ts.state_now == robot_api.MOTORS_IDLE and not rig.ts.fell_off_board
              and not rig.robot.active() & rig.ts.leg_controllers, f"got {res}; state {rig.ts.state_now}")
        res = await rig.call("move")
        check("... and afterwards walking tools are no longer refused for the board", "skateboard" not in res.message,
              f"got {res}")
    finally:
        rig.close()

    text = prompt_sections("Skateboard").lower()
    check("prompt.md has a Skateboard section that says a person places Pupper", "place" in text, text[:200])
    check("... that stop comes first", "stop" in text)
    check("... and that distances are estimates and the path must be clear", "estimate" in text and "clear" in text)
    check('... where Pupper stands ("beside the board", "castle")', "beside the board" in text and "castle" in text, text)
    check('... and that long rides drift ("drift")', "drift" in text, text)
    flat = " ".join(text.split())
    placed = ("a person has to place you: beside the board on its right, facing the end with the castle picture, your"
              " left feet on the deck's middle line, one ahead of the middle and one behind, and your right feet on the"
              " floor.")
    check("... the placement sentence is exact", placed in flat, flat[:400], passes=placed)
    drift = ("on a long ride you also drift a little to one side, so keep rides short. if the board has wandered, call"
             " stop first, then ask a person to hold you and straighten the board.")
    check("... the drift sentence is exact", drift in flat, flat, passes=drift)
    sentence = next((s for s in re.split(r"(?<=\.) ", flat) if "wandered" in s), "")
    check('... and before anyone straightens the board, it says "call stop first"', "call stop first" in sentence,
          sentence)
    queued = re.search(r"at most (\d+(?:\.\d+)?) s of riding", text)
    limit = yaml.safe_load((skateboard.SKATE_DIR / "skate.yaml").read_text())["queue_max_seconds"]
    check("... and its queue limit is skate.yaml's queue_max_seconds", bool(queued) and float(queued[1]) == float(limit),
          f"prompt: {queued[0] if queued else 'no at most ... s of riding'}; skate.yaml: {limit}")


def _load_server():
    """robot/server.py with stand-ins for its web and camera libraries (the desk tests
    don't install them), so its debug endpoints can be called like the web app does."""
    class _App:
        def __init__(self, *a, **k):
            pass

        def _route(self, *a, **k):
            return lambda fn: fn

        get = post = websocket = _route

        def add_middleware(self, *a, **k):
            pass

        def mount(self, *a, **k):
            pass

    class _BaseModel:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    stubs = {
        "cv2": {}, "numpy": {}, "psutil": {}, "uvicorn": {},
        "fastapi": {"Body": lambda default=None: default, "FastAPI": _App, "HTTPException": Exception,
                    "Response": object, "WebSocket": object, "WebSocketDisconnect": Exception},
        "fastapi.middleware": {}, "fastapi.middleware.cors": {"CORSMiddleware": object},
        "fastapi.staticfiles": {"StaticFiles": _App},
        "pydantic": {"BaseModel": _BaseModel, "Field": lambda *a, **k: None},
        "fisheye_converter": {"equirect_height": None, "fisheye_to_equirectangular": None, "load_camera_model": None},
    }
    saved = {name: sys.modules.get(name) for name in [*stubs, "server"]}
    try:
        for name, attrs in stubs.items():
            m = types.ModuleType(name)
            m.__dict__.update(attrs)
            sys.modules[name] = m
        import importlib.util
        spec = importlib.util.spec_from_file_location("server", HERE / "server.py")
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        return server
    finally:
        for name, m in saved.items():
            if m is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = m


async def skate_guards() -> None:
    print("Skate: the board guard holds on every path")
    rig = Rig(skate=fixture_skate())
    try:
        # A stop while start_skating's controller switch is still being answered: the
        # switch goes through anyway, so Pupper is on the board in skate mode.
        await rig.call("lie_down")
        rig.robot.switch_delay = 0.5
        await tools.call(rig.ts, "start_skating", {"placed": True})
        for _ in range(10_000):
            if SKATE_CONTROLLER in rig.robot.active():
                break
            await _real_sleep(0)
        res = await rig.call("stop")
        rig.robot.switch_delay = 0.0
        check("a stop during start_skating's switch: the skate policy holds the legs, and Pupper knows it",
              SKATE_CONTROLLER in rig.robot.active() and rig.ts.state_now == SKATING
              and rig.ts.state_after_queue == SKATING, f"got {res}; state {rig.ts.state_now}/{rig.ts.state_after_queue}")
        check("... and stop's reply says Pupper is on the board in skate mode", "skate mode" in res.message, res.message)
        switches = len(rig.robot.switches)
        res = await rig.call("stand_up")
        check("... so stand_up is refused for the board, and nothing switched",
              not res.ok and "skateboard" in res.message and len(rig.robot.switches) == switches, f"got {res}")
        rig.ts.state_now = rig.ts.state_after_queue = robot_api.MOTORS_IDLE  # as if the switch landed after stop looked
        res = await rig.ts.switch_to(LOCOMOTION_CONTROLLER)
        check("switch_to refuses another policy while the skate controller is active, whatever the state says",
              not res.ok and "skateboard" in res.message and len(rig.robot.switches) == switches
              and rig.ts.state_now == SKATING, f"got {res}; state {rig.ts.state_now}")
        rig.ts.state_after_queue = SKATING
        res = await rig.call("end_skating", held=True)
        check("... and end_skating still gets Pupper off", res.ok and rig.ts.state_now == robot_api.MOTORS_IDLE
              and SKATE_CONTROLLER not in rig.robot.active(), f"got {res}")

        # The controller manager refuses the skate switch: Pupper never got on.
        rig.robot.refuse = {SKATE_CONTROLLER}
        await rig.call("start_skating", placed=True)
        check("a start_skating whose switch fails leaves Pupper in motors idle, not skating",
              rig.ts.state_now == robot_api.MOTORS_IDLE and rig.ts.state_after_queue == robot_api.MOTORS_IDLE,
              f"state {rig.ts.state_now}/{rig.ts.state_after_queue}")
        res = await rig.call("skate_forward")
        check("... skate_forward then says Pupper is not in skate mode", not res.ok and "not in skate mode" in res.message,
              f"got {res}")
        res = await tools.call(rig.ts, "start_skating", {"placed": True})
        check("... start_skating does not claim it is already skating", "already" not in res.message, f"got {res}")
        await rig.settle()
        rig.robot.refuse = set()
        res = await rig.call("stand_up")
        check("... and stand_up is not refused for the board", res.ok and rig.ts.state_now == LOCOMOTION, f"got {res}")

        # Start queued but not run yet: the registry already treats Pupper as on the board.
        await rig.call("lie_down")
        await tools.call(rig.ts, "start_skating", {"placed": True})
        queued = rig.ts.state_now == robot_api.MOTORS_IDLE and rig.ts.state_after_queue == SKATING
        res = await tools.call(rig.ts, "stand_up", {})
        check("with start_skating queued but not run, stand_up is refused up front",
              queued and not res.ok and "skateboard" in res.message, f"got {res}; queued {queued}")
        await rig.settle()
        check("... and start_skating then runs", rig.ts.state_now == SKATING)

        # The web app's debug endpoints go through the same board guard.
        server = _load_server()
        server.tools = rig.ts
        switches, mark = len(rig.robot.switches), len(rig.robot.twists())
        for what, endpoint, args in [
            ("/move/velocity", server.move_velocity, [server.MoveVelocity(forward_backward_velocity=0.3,
                                                                          right_left_velocity=0.0, turning_velocity=0.0,
                                                                          duration=2.0)]),
            ("/move/direction", server.move_direction, [server.MoveDirection(heading=90.0, speed=0.3, duration=2.0)]),
            ("/animation", server.animation, [server.Trick(animation_name="bow")]),
            ("/interactive_action", server.interactive_action, [server.TrickAction(action="wave_left")]),
            ("/walking/activate", server.walking_activate, []),
            ("/walking/deactivate", server.walking_deactivate, []),
        ]:
            out = await endpoint(*args)
            await rig.settle()
            check(f"on the board, the debug endpoint {what} is refused, and says why",
                  not out["success"] and "skateboard" in out["message"] and rig.ts.state_after_queue == SKATING,
                  f"got {out}; state after queue {rig.ts.state_after_queue}")
        check("... none of them moved the legs or sent a speed", len(rig.robot.switches) == switches
              and not walk_twists_since(rig, mark) and SKATE_CONTROLLER in rig.robot.active())
        out = await server.stop_immediate()
        check("... the stop endpoint still works on the board", out["success"] and rig.ts.state_now == SKATING, f"got {out}")

        # A walk queued past the guards (as the debug endpoints could before): its job is
        # refused, and the state Pupper will be in goes back to skating.
        await behaviors.walk(rig.ts, 0.3, 0.0, 0.0, 2.0)
        await rig.settle()
        check("a walk that got queued on the board fails, and Pupper is still known to be skating",
              rig.ts.state_now == SKATING and rig.ts.state_after_queue == SKATING and len(rig.robot.switches) == switches,
              f"state {rig.ts.state_now}/{rig.ts.state_after_queue}")

        # stop on the board: stays in skate mode, guard on. The gamepad e-stop: relaxes, guard stays on.
        res = await rig.call("stop")
        res2 = await rig.call("stand_up")
        check("stop on the board keeps skate mode (balancing), and the board guard holds",
              res.ok and rig.ts.state_now == SKATING and SKATE_CONTROLLER in rig.robot.active()
              and not res2.ok and "skateboard" in res2.message, f"got {res}; {res2}")
        await tools.call(rig.ts, "skate_forward", {"seconds": 5})
        for _ in range(40):
            await _real_sleep(0)
        rig.robot.send("/emergency_stop", _DataMsg())
        await rig.settle()
        check("the gamepad e-stop while riding relaxes the motors: the skate controller is off",
              rig.ts.state_now == robot_api.MOTORS_IDLE and rig.ts.state_after_queue == robot_api.MOTORS_IDLE
              and not rig.robot.active() & rig.ts.leg_controllers,
              f"state {rig.ts.state_now}; active {rig.robot.active()}")
        last = rig.robot.twists()[-1][1]
        check("... the last speed command is zero", last.linear.x == 0 and last.angular.z == 0)
        check("... and Pupper is still known to be on the board", rig.ts.fell_off_board)
        switches, mark = len(rig.robot.switches), len(rig.robot.twists())
        for name, args in [("stand_up", {}), ("move", {"direction": "forward", "speed": 0.3, "duration": 2}),
                           ("follow", {"enabled": True}), ("play_trick", {"name": "bow"})]:
            res = await rig.call(name, **args)
            check(f"... {name} is refused for the board, and says why", not res.ok and "skateboard" in res.message,
                  f"got {res}")
        out = await server.walking_activate()
        await rig.settle()
        check("... so is the debug endpoint /walking/activate", not out["success"] and "skateboard" in out["message"],
              f"got {out}")
        check("... none of them moved the legs or sent a speed", len(rig.robot.switches) == switches
              and not walk_twists_since(rig, mark) and not rig.robot.active() & rig.ts.leg_controllers)
        res = await rig.call("robot_status")
        check("... robot_status says the motors are relaxed on the board: end_skating with held true, then stand_up",
              "relaxed" in res.message and "board" in res.message and "end_skating" in res.message
              and "held" in res.message and "stand_up" in res.message, res.message)
        res = await rig.call("start_skating", placed=True)
        check("... start_skating with placed=true takes the legs back", res.ok and rig.ts.state_now == SKATING
              and SKATE_CONTROLLER in rig.robot.active(), f"got {res}; state {rig.ts.state_now}")
        rig.robot.send("/emergency_stop", _DataMsg())
        await rig.settle()
        res = await rig.call("end_skating", held=True)
        check("... after another e-stop, end_skating with held=true ends it and releases the guard",
              res.ok and rig.ts.state_now == robot_api.MOTORS_IDLE and not rig.ts.fell_off_board
              and not rig.robot.active() & rig.ts.leg_controllers, f"got {res}; state {rig.ts.state_now}")
        res = await rig.call("stand_up")
        check("... and then stand_up works", res.ok and rig.ts.state_now == LOCOMOTION, f"got {res}")
        await rig.ts.wait(3.0)  # past the e-stop's 2 s window
        rig.robot.send("/emergency_stop", _DataMsg())
        await rig.settle()
        res = await rig.call("stand_up")
        check("the gamepad e-stop off the board leaves no board guard: stand_up works after it",
              res.ok and rig.ts.state_now == LOCOMOTION and not rig.ts.fell_off_board, f"got {res}")

        # Two end_skating calls while a ride is still queued.
        await _on_board(rig)
        await tools.call(rig.ts, "skate_forward", {"seconds": 3})
        first = await tools.call(rig.ts, "end_skating", {"held": True})
        second = await tools.call(rig.ts, "end_skating", {"held": True})
        await rig.settle()
        check("a second end_skating while one is queued is not 'nothing to end'",
              first.ok and second.ok and "nothing" not in second.message, f"got {first}; {second}")
        check("... and Pupper ends in motors idle", rig.ts.state_now == robot_api.MOTORS_IDLE
              and not rig.robot.active() & rig.ts.leg_controllers)

        # After a fall on the board, what Gemini hears names end_skating, not stand_up first.
        await _on_board(rig)
        rig.robot.send("/rosout", _Msg(name=SKATE_CONTROLLER, msg=robot_api.ESTOP_LOG))
        await rig.settle()
        res = await rig.call("robot_status")
        check("after a fall on the board, robot_status says to end_skating with held true",
              "end_skating" in res.message and "held" in res.message and "fell" in res.message, res.message)
        res = await rig.call("skate_forward")
        check("... and so does skate_forward's refusal", not res.ok and "end_skating" in res.message
              and "held" in res.message, f"got {res}")
        await rig.call("end_skating", held=True)
    finally:
        rig.close()

    rig = Rig(skate=fixture_skate())
    try:
        # The debug panel's STOP button sends /move/velocity with everything 0, and the app's
        # disconnect sends /walking/deactivate. On the board both stop the ride (like the stop
        # tool), and the skate policy keeps balancing: they never relax the motors there.
        server = _load_server()
        server.tools = rig.ts

        def zero_move():
            return server.MoveVelocity(forward_backward_velocity=0.0, right_left_velocity=0.0,
                                       turning_velocity=0.0, duration=0.0)

        replies = {}
        for what, endpoint, args in [("the debug panel's STOP (/move/velocity, all 0)", server.move_velocity, [zero_move()]),
                                     ("the app's disconnect (/walking/deactivate)", server.walking_deactivate, [])]:
            await _on_board(rig)
            before = len(rig.robot.twists())
            await tools.call(rig.ts, "skate_forward", {"seconds": 5})
            for _ in range(40):
                await _real_sleep(0)
            riding = bool(_ride_twists(rig, before))
            switches, mark = len(rig.robot.switches), len(rig.robot.twists())
            out = await endpoint(*args)
            await rig.settle()
            replies[what] = out
            last = rig.robot.twists()[-1][1]
            ride = _ride_twists(rig, before)
            check(f"on the board, {what} cuts the ride short: the queue is empty and a zero command was sent",
                  riding and 0 < len(ride) < 90 and rig.ts.commands.empty() and rig.ts.current is None
                  and len(rig.robot.twists()) > mark and last.linear.x == 0 and last.angular.z == 0,
                  f"got {out}; {len(ride)} ride commands of 100")
            check("... and the skate policy still holds the legs (motors not relaxed)",
                  SKATE_CONTROLLER in rig.robot.active() and rig.ts.state_now == SKATING
                  and len(rig.robot.switches) == switches, f"state {rig.ts.state_now}; active {rig.robot.active()}")
            await rig.call("end_skating", held=True)
        await _on_board(rig)
        out = replies["the debug panel's STOP (/move/velocity, all 0)"]
        check("... the STOP reply is ok and says it stopped", out["success"] and "stopped" in out["message"], f"got {out}")
        out = replies["the app's disconnect (/walking/deactivate)"]
        check("... the disconnect reply says Pupper is still balancing on the board, and end_skating with a person"
              " holding it is the way off", "balancing" in out["message"] and "board" in out["message"]
              and "end_skating" in out["message"] and "hold" in out["message"], f"got {out}")
        for vx, vy, wz, dur in [(0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 10.0, 0.0), (0.0, 0.1, 0.0, 0.0)]:
            switches, mark = len(rig.robot.switches), len(rig.robot.twists())
            out = await server.move_velocity(server.MoveVelocity(forward_backward_velocity=vx, right_left_velocity=vy,
                                                                 turning_velocity=wz, duration=dur))
            await rig.settle()
            check(f"on the board, /move/velocity ({vx}, {vy}, {wz}, {dur} s) is still refused, and says why",
                  not out["success"] and "skateboard" in out["message"] and len(rig.robot.switches) == switches
                  and not walk_twists_since(rig, mark) and rig.ts.state_now == SKATING, f"got {out}")
        await rig.call("end_skating", held=True)
    finally:
        rig.close()

    # Off the board, STOP and disconnect do what they did before: the same replies as on a
    # robot with no skate policy installed.
    server = _load_server()

    async def off_board(skate, endpoint: str, args: list):
        rig = Rig(skate=skate)
        try:
            server.tools = rig.ts
            out = await getattr(server, endpoint)(*args)
            await rig.settle()
            return out, rig.ts.state_now
        finally:
            rig.close()

    zero = server.MoveVelocity(forward_backward_velocity=0.0, right_left_velocity=0.0, turning_velocity=0.0, duration=0.0)
    for what, endpoint, args in [("the STOP /move/velocity (all 0)", "move_velocity", [zero]),
                                 ("/walking/deactivate", "walking_deactivate", [])]:
        plain = await off_board(None, endpoint, args)
        skate = await off_board(fixture_skate(), endpoint, args)
        check(f"off the board, {what} behaves as on a robot without skate", plain == skate, f"{plain} vs {skate}")

    # GET /system/status reports skate mode (the battery and network parts are stood in for).
    host = {"battery": {"percentage": 80.0, "is_charging": False, "voltage": 8.1},
            "cpu": {"usage": 5.0, "temperature": 50.0}, "services": {"robot": "active", "internet": "offline"}}

    async def status(rig: Rig | None) -> dict | None:
        if not hasattr(server, "host_status"):
            return None
        server.tools = rig.ts if rig else None
        server.host_status = lambda: dict(host)
        out = server.system_status()
        return await out if inspect.isawaitable(out) else out

    rig = Rig(skate=fixture_skate())
    try:
        out = await status(rig)
        check("/system/status keeps its battery, cpu and services keys, and adds skate",
              out is not None and set(out) == {*host, "skate"} and all(out[k] == v for k, v in host.items()), f"got {out}")
        want = {"installed": True, "policy": "steer_v6", "tested": "robot 2026-11-26: a test entry", "error": None}
        check("... with a skate policy installed: installed, its name and test note, no error, controller inactive",
              out is not None and out.get("skate") == {**want, "controller": "inactive"}, f"got {out and out.get('skate')}")
        await _on_board(rig)
        out = await status(rig)
        check("... and after start_skating the controller is active",
              out is not None and out.get("skate") == {**want, "controller": "active"}, f"got {out and out.get('skate')}")
        await rig.call("end_skating", held=True)
    finally:
        rig.close()
    rig = Rig(skate_error="skate.yaml: speed must be a number")
    try:
        out = await status(rig)
        sk = out and out.get("skate")
        check("... with a broken skate config: not installed, and the error says why",
              bool(sk) and sk["installed"] is False and sk["policy"] is None
              and sk["error"] == "skate.yaml: speed must be a number", f"got {sk}")
    finally:
        rig.close()
    rig = Rig()
    try:
        out = await status(rig)
        sk = out and out.get("skate")
        check("... with no skate config: not installed, no error, controller missing",
              sk == {"installed": False, "policy": None, "tested": None, "error": None, "controller": "missing"}, f"got {sk}")
    finally:
        rig.close()

    # The status bar polls /system/status: it must never wait on the controller manager.
    rig = Rig(skate=fixture_skate())
    try:
        calls = []
        real_states = rig.ts.controller_states

        async def counted():
            calls.append(1)
            return await real_states()

        rig.ts.controller_states = counted
        rig.robot.unready = {"/controller_manager/list_controllers"}
        await rig.settle()
        calls.clear()
        out = await status(rig)
        check("with the controller manager not ready, /system/status answers at once: controller unknown,"
              " battery and cpu still there, and controller_states was not called",
              out is not None and out.get("skate", {}).get("controller") == "unknown" and out.get("battery") == host["battery"]
              and out.get("cpu") == host["cpu"] and not calls, f"got {out}; {len(calls)} calls")
        rig.robot.unready = set()

        async def broken():
            raise RuntimeError("controller_manager went away")

        rig.ts.controller_states = broken
        out = await status(rig)
        check("if asking the controller manager fails, controller is unknown and battery and cpu still come back",
              out is not None and out.get("skate", {}).get("controller") == "unknown"
              and out.get("battery") == host["battery"] and out.get("cpu") == host["cpu"], f"got {out}")
    finally:
        rig.close()
    out = await status(None)
    check("before the robot is ready (tools None), the skate block says not installed, controller unknown",
          out is not None and out.get("skate") == {"installed": False, "policy": None, "tested": None, "error": None,
                                                    "controller": "unknown"}, f"got {out and out.get('skate')}")

    # Disconnect when Pupper is on the board but cannot ride, and while start_skating is only queued.
    rig = Rig(skate=fixture_skate())
    try:
        server.tools = rig.ts
        await _on_board(rig)
        rig.robot.send("/emergency_stop", _DataMsg())
        await rig.settle()
        out = await server.walking_deactivate()
        await rig.settle()
        want = "stopped, but Pupper did not lie down. Its " + skateboard.STOPPED_ON_BOARD_MESSAGE
        check("disconnect after the e-stop on the board says the motors relaxed there and names end_skating with held",
              out == {"success": False, "message": want}, f"got {out}")
        res = await rig.call("start_skating", placed=True)
        rig.robot.send("/rosout", _Msg(name=SKATE_CONTROLLER, msg=robot_api.ESTOP_LOG))
        await rig.settle()
        rig.imu(tilt_deg=3)
        out = await server.walking_deactivate()
        await rig.settle()
        want = "stopped, but Pupper did not lie down. " + skateboard.FELL_ON_BOARD_MESSAGE
        check("disconnect after a fall on the board says it fell and names end_skating with held",
              res.ok and out == {"success": False, "message": want}, f"got {out}")
        await rig.call("end_skating", held=True)
        await tools.call(rig.ts, "start_skating", {"placed": True})  # queued, not run yet
        queued = rig.ts.state_now == robot_api.MOTORS_IDLE and rig.ts.state_after_queue == SKATING
        out = await server.walking_deactivate()
        await rig.settle()
        want = "stopped; start_skating was cancelled before it began. lie down queued; the queue should finish in 1.0 s"
        check("disconnect while start_skating is only queued cancels it and says so, then lies down as usual",
              queued and out == {"success": True, "message": want}
              and rig.ts.state_now == robot_api.MOTORS_IDLE and SKATE_CONTROLLER not in rig.robot.active(), f"got {out}")
    finally:
        rig.close()


async def _ride_mid(rig: Rig, tool: str, args: dict) -> None:
    """Queue a ride and let it run for a moment (about 2 s of fake time), without waiting for it to end."""
    await tools.call(rig.ts, tool, args)
    for _ in range(40):
        await _real_sleep(0)


def _rides(twists) -> list[tuple[float, float]]:
    """Each run of non-zero speed commands, as (start, end): end is the next zero command
    (inf if none came, so a ride left running counts as too long)."""
    runs, start = [], None
    for t, m in twists:
        moving = abs(m.linear.x) + abs(m.linear.y) + abs(m.angular.z) > 1e-9
        if moving and start is None:
            start = t
        elif not moving and start is not None:
            runs.append((start, t))
            start = None
    if start is not None:
        runs.append((start, math.inf))
    return runs


async def _demo(cfg: SkateConfig) -> None:
    """The live demo, line by line, on the fake robot, for one catalogue entry."""
    p = f"[{cfg.name}]"
    rig = Rig(skate=cfg)
    try:
        # 1. A person lies Pupper down and places it; Gemini starts skate mode.
        await rig.call("lie_down")
        check(f"{p} 1. Pupper starts from motors idle", rig.ts.state_now == robot_api.MOTORS_IDLE, f"state {rig.ts.state_now}")
        res = await rig.call("start_skating", placed=False)
        check(f"{p} 1. start_skating(placed=false) is refused with the placement text",
              not res.ok and behaviors.PLACEMENT in res.message and not rig.robot.activations(SKATE_CONTROLLER), f"got {res}")
        res = await rig.call("start_skating", placed=True)
        check(f"{p} 1. start_skating(placed=true) starts skate mode", res.ok and rig.ts.state_now == SKATING
              and SKATE_CONTROLLER in rig.robot.active(), f"got {res}; state {rig.ts.state_now}")
        res = await rig.call("robot_status")
        check(f"{p} 1. robot_status says skating and names the policy",
              "skating" in res.message and f"(policy {cfg.name})" in res.message, res.message)

        # 2. Walking on the board.
        switches = len(rig.robot.switches)
        for name, args in [("move", {"direction": "forward", "speed": 0.3, "duration": 2}), ("stand_up", {})]:
            res = await rig.call(name, **args)
            check(f"{p} 2. {name} is refused with the on-board message", not res.ok
                  and skateboard.ON_BOARD_MESSAGE in res.message and len(rig.robot.switches) == switches, f"got {res}")

        # 3. "Go forward."
        mark = len(rig.robot.twists())
        res = await rig.call("skate_forward")
        ride = _rides(rig.robot.twists()[mark:])
        metres = f"about {cfg.travel_m_per_s * cfg.default_seconds:.1f} m forward"
        check(f"{p} 3. skate_forward rides default_seconds ({cfg.default_seconds:g} s)", res.ok and len(ride) == 1
              and abs(ride[0][1] - ride[0][0] - cfg.default_seconds) < 0.06, f"got {res}; rides {ride}")
        check(f"{p} 3. ... and its reply gives travel_m_per_s x default_seconds ({metres}) as an estimate",
              metres in res.message and "Estimate" in res.message, res.message, passes=metres)

        # 4. "Turn left gently."
        mark = len(rig.robot.twists())
        res = await rig.call("skate_turn", direction="left", sharpness="gentle", seconds=3)
        ride = _rides(rig.robot.twists()[mark:])
        degrees = f"curving left by about {cfg.turn_deg_per_s['gentle'] * 3:.0f} degrees"
        check(f"{p} 4. skate_turn left gentle 3 s rides, carving at +{cfg.carve['gentle']:g} rad/s", res.ok and len(ride) == 1
              and all(abs(m.angular.z - cfg.carve["gentle"]) < 1e-3 for _, m in _ride_twists(rig, mark)), f"got {res}")
        check(f"{p} 4. ... and its reply gives turn_deg_per_s gentle x 3 ({degrees})", degrees in res.message
              and "Estimate" in res.message, res.message, passes=degrees)

        # 5. "Stop" in the middle of a ride.
        mark = len(rig.robot.twists())
        await _ride_mid(rig, "skate_forward", {"seconds": 5})
        res = await rig.call("stop")
        ride = _rides(rig.robot.twists()[mark:])
        last = rig.robot.twists()[-1][1]
        check(f"{p} 5. stop mid-ride cuts the 5 s ride short", res.ok and len(ride) == 1 and ride[0][1] - ride[0][0] < 4.5,
              f"got {res}; rides {ride}")
        check(f"{p} 5. ... the last command sent is zero", last.linear.x == 0 and last.linear.y == 0
              and last.angular.z == 0, f"last vx {last.linear.x:g}, wz {last.angular.z:g}")
        check(f"{p} 5. ... and the skate controller still holds the legs", rig.ts.state_now == SKATING
              and rig.robot.active() & rig.ts.leg_controllers == {SKATE_CONTROLLER}, f"active {rig.robot.active()}")

        # 6. The gamepad e-stop while riding, then back on.
        await _ride_mid(rig, "skate_forward", {"seconds": 5})
        rig.robot.send("/emergency_stop", _DataMsg())
        await rig.settle()
        check(f"{p} 6. gamepad e-stop while riding: motors relaxed", rig.ts.state_now == robot_api.MOTORS_IDLE
              and not rig.robot.active() & rig.ts.leg_controllers, f"active {rig.robot.active()}")
        res = await rig.call("stand_up")
        check(f"{p} 6. ... stand_up is refused (still on the board)", not res.ok and "skateboard" in res.message
              and not rig.robot.active() & rig.ts.leg_controllers, f"got {res}")
        res = await rig.call("start_skating", placed=True)
        check(f"{p} 6. ... start_skating(placed=true) works again", res.ok and rig.ts.state_now == SKATING
              and SKATE_CONTROLLER in rig.robot.active(), f"got {res}; state {rig.ts.state_now}")

        # 7. A tilt fall while skating: the skate policy's tilt e-stop.
        await rig.ts.wait(3.0)  # past the gamepad e-stop's 2 s window
        await _ride_mid(rig, "skate_forward", {"seconds": 5})
        rig.imu(tilt_deg=60)
        rig.robot.send("/rosout", _Msg(name=SKATE_CONTROLLER, msg=robot_api.ESTOP_LOG))
        await rig.settle()
        check(f"{p} 7. a tilt fall while skating is a fall from the board", rig.ts.state_now == robot_api.FALLEN
              and rig.ts.fell_off_board, f"state {rig.ts.state_now}; fell_off_board {rig.ts.fell_off_board}")
        switches = len(rig.robot.switches)
        res = await rig.call("move", direction="forward", speed=0.3, duration=2)
        check(f"{p} 7. ... walking is refused", not res.ok and "skateboard" in res.message
              and len(rig.robot.switches) == switches, f"got {res}")
        res = await rig.call("end_skating", held=False)
        check(f"{p} 7. ... end_skating(held=false) is refused", not res.ok and "hold" in res.message
              and len(rig.robot.switches) == switches, f"got {res}")
        res = await rig.call("end_skating", held=True)
        check(f"{p} 7. ... end_skating(held=true) relaxes the motors", res.ok and rig.ts.state_now == robot_api.MOTORS_IDLE
              and not rig.ts.fell_off_board and not rig.robot.active() & rig.ts.leg_controllers,
              f"got {res}; state {rig.ts.state_now}")
        rig.imu(tilt_deg=3)  # a person sets Pupper upright on the floor
        res = await rig.call("stand_up")
        check(f"{p} 7. ... set upright on the floor, stand_up works", res.ok and rig.ts.state_now == LOCOMOTION, f"got {res}")

        # 8. Every command sent while on the board stayed inside the limits.
        sent = [m for _, m in rig.robot.twists()]
        rides = _rides(rig.robot.twists())
        check(f"{p} 8. no command was over 0.3 m/s or a yaw of 0.5 rad/s, or sideways",
              bool(sent) and all(m.linear.x <= 0.3 + 1e-9 and abs(m.angular.z) <= 0.5 + 1e-6 and m.linear.y == 0 for m in sent),
              f"max vx {max(m.linear.x for m in sent):g}; max |wz| {max(abs(m.angular.z) for m in sent):g}")
        check(f"{p} 8. ... no ride lasted over 5 s, and each ended with a zero command",
              len(rides) == 5 and all(end - start <= 5.0 + 1e-6 for start, end in rides),
              f"rides (s): {[round(end - start, 2) for start, end in rides]}")
    except Exception as e:  # noqa: BLE001
        check(f"{p} the demo ran to the end", False, f"raised {type(e).__name__}: {e}")
    finally:
        rig.close()


async def skate_demo() -> None:
    print("Skate: the live demo, scripted, for every policy in skate/skate.yaml")
    spec = yaml.safe_load((skateboard.SKATE_DIR / "skate.yaml").read_text())
    for name in spec["policies"]:
        d = Path(tempfile.mkdtemp(prefix="gemini_pupper_skate_"))
        (d / "skate.yaml").write_text(yaml.safe_dump({**spec, "policy": name}))
        (d / "policies").symlink_to(skateboard.SKATE_DIR / "policies")
        err = _load_error(d)
        if not check(f"[{name}] the catalogue entry loads", not err, f"error: {err!r}"):
            continue
        before = FAIL
        await _demo(load_skate(d))
        check(f"[{name}] the whole demo passes", FAIL == before, f"{FAIL - before} step(s) failed for {name}")


def walk_twists_since(rig: Rig, start: int):
    return [m for _, m in rig.robot.twists()[start:] if abs(m.linear.x) + abs(m.linear.y) + abs(m.angular.z) > 1e-9]


# ----------------------------------------------------------------------------- main
GROUPS = {
    "todo1": todo1, "todo2": todo2, "todo3": todo3, "todo4": todo4, "todo5": todo5, "todo6": todo6,
    "todo7": todo7, "todo8": todo8, "todo9": todo9, "todo10": todo10, "todo11": todo11, "robot": provided,
    "optional_jump": optional_jump, "skate_config": skate_config, "skate_robot": skate_robot,
    "skate_tools": skate_tools, "skate_guards": skate_guards, "skate_catalogue": skate_catalogue,
    "skate_demo": skate_demo,
}
OPT_IN = {"optional_jump"}  # run only when asked for by name
PARTS = {
    "part1": ["todo1"],
    "part2": ["todo2", "todo3", "todo4"],
    "part3": ["todo5", "todo6"],
    "part4": ["todo7", "todo8"],
    "part5": ["todo9", "todo10"],
    "part7": ["todo11"],
    "skate": ["skate_config", "skate_catalogue", "skate_robot", "skate_tools", "skate_guards", "skate_demo"],
}


def main() -> None:
    chosen: list[str] = []
    for arg in sys.argv[1:] or ["robot", *(g for g in GROUPS if g not in OPT_IN)]:
        for name in PARTS.get(arg, [arg]):
            if name not in GROUPS:
                sys.exit(f"unknown test group {arg!r}; choose from {list(GROUPS)} or {list(PARTS)}")
            if name not in chosen:
                chosen.append(name)
    for name in chosen:
        asyncio.run(GROUPS[name]())
        print()
    if FAIL:
        print(f"{FAIL} check(s) failed, {PASS} passed.")
        raise SystemExit(1)
    if TODO:
        print(f"All {PASS} checks that ran passed; {TODO} TODO(s) not written yet. Keep going!")
        raise SystemExit(1)
    print(f"All {PASS} checks passed. Now try it on the robot (with a TA's OK for new policies).")


if __name__ == "__main__":
    main()
