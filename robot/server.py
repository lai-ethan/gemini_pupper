"""Gemini Pupper robot server: HTTP + WebSocket API on port 8000, and the web app.

Run on the Pupper after the robot stack is up (see start.sh):
    python3 robot/server.py

The web app gets Gemini's instructions and tools from GET /tools (lab/prompt.md
and the @tool functions in lab/) and sends every tool call to POST /tools/<name>. Every call
returns {"success": bool, "message": str}, which goes straight back to Gemini,
so Gemini hears about refused speeds, unknown tricks and failed controller
switches. The other endpoints are for the debug panel and for testing by hand.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import numpy as np
import psutil
import uvicorn
from fastapi import Body, FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import tool_registry as gemini_tools
from fisheye_converter import equirect_height, fisheye_to_equirectangular, load_camera_model
from robot_api import LIMITS, Robot
from skateboard import FELL_ON_BOARD_MESSAGE, SKATE_CONTROLLER, SKATING, STOPPED_ON_BOARD_MESSAGE

behaviors = gemini_tools.load_lab()  # lab/: every tool, and the behaviors they call

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("server")

REPO = Path(__file__).resolve().parent.parent
WEB_DIST = REPO / "web" / "dist"
PROMPT_FILE = REPO / "lab" / "prompt.md"
BATTERY_SCRIPT = Path("/home/pi/pupperv3-monorepo/robot/utils/check_batt_voltage.py")

tools: Robot | None = None


class Detections:
    """Forwards person detections (ZMQ on localhost:5556) to browser WebSockets.

    The follower publishes them while it follows (it keeps the Hailo off
    otherwise); the monorepo detector, when used instead, publishes them always.
    """

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def run(self) -> None:
        try:
            import zmq
            import zmq.asyncio
        except ImportError:
            log.warning("pyzmq missing; eye tracking from robot detections disabled")
            return
        sock = zmq.asyncio.Context().socket(zmq.SUB)
        sock.connect("tcp://127.0.0.1:5556")
        sock.subscribe(b"")
        while True:
            payload = (await sock.recv_multipart())[-1].decode("utf-8", errors="ignore")
            if payload.startswith("{"):
                for ws in list(self.clients):
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        self.clients.discard(ws)


detections = Detections()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tools
    tools = Robot()
    task = asyncio.create_task(detections.run())
    yield
    task.cancel()


app = FastAPI(title="Gemini Pupper robot server", lifespan=lifespan)
# The web app is served from this same origin; also allow a dev server on the laptop.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)


def T() -> Robot:
    if tools is None:
        raise HTTPException(503, "tool server not ready")
    return tools


# ------------------------------------------------------------------ models
class MoveVelocity(BaseModel):
    forward_backward_velocity: float = Field(..., description="m/s, positive = forward")
    right_left_velocity: float = Field(..., description="m/s, positive = right")
    turning_velocity: float = Field(..., description="deg/s, positive = turn right")
    duration: float = Field(..., description="seconds")


class MoveDirection(BaseModel):
    heading: float = Field(..., description="degrees, 0 = forward, 90 = right")
    speed: float
    duration: float


class Trick(BaseModel):
    animation_name: str


class TrickAction(BaseModel):
    action: str


class Wait(BaseModel):
    duration: float


class Volume(BaseModel):
    volume: int = Field(..., ge=0, le=150)


# ------------------------------------------------------------------ Gemini's tools
def system_prompt() -> str:
    """lab/prompt.md without its <!-- comments -->."""
    text = PROMPT_FILE.read_text() if PROMPT_FILE.exists() else ""
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


@app.get("/tools")
def tool_list():
    """What Gemini is told: the system prompt and the tool declarations."""
    return {"prompt": system_prompt(), "tools": gemini_tools.declarations(T())}


@app.post("/tools/{name}")
async def tool_call(name: str, args: dict = Body(default={})):
    res = await gemini_tools.call(T(), name, args)
    log.info("tool %s(%s) -> %s: %s", name, args, "ok" if res.ok else "FAILED", res.message)
    return res.as_dict()


# ------------------------------------------------------------------ commands
# The debug panel's buttons run the same behaviors as Gemini's tools, so they get the
# same board guard: on the skateboard each is refused like the tool it stands for.
@app.post("/walking/activate")
async def walking_activate():
    refused = gemini_tools.on_board_refusal(T(), "stand_up")
    return (refused or await behaviors.stand_up(T())).as_dict()


# Two buttons still work on the board, as the stop tool does there: the panel's STOP (a
# /move/velocity of all zeros) and the app's disconnect (/walking/deactivate). They stop the
# ride and the skate policy keeps balancing; only end_skating, with Pupper held, relaxes it.
@app.post("/walking/deactivate")
async def walking_deactivate():
    refused = gemini_tools.on_board_refusal(T(), "lie_down")  # on the board, end_skating (with held) relaxes
    if not refused:
        return (await behaviors.lie_down(T())).as_dict()
    robot = T()
    await robot.immediate_stop(relax_playing_trick=True)
    if not gemini_tools.on_board_refusal(robot, "lie_down"):  # start_skating was only queued: the stop cancelled it
        res = await behaviors.lie_down(robot)
        return {"success": res.ok, "message": f"stopped; start_skating was cancelled before it began. {res.message}"}
    if robot.fallen:
        return {"success": False, "message": f"stopped, but Pupper did not lie down. {FELL_ON_BOARD_MESSAGE}"}
    if robot.state_now != SKATING:  # the e-stop relaxed the motors on the board
        return {"success": False, "message": f"stopped, but Pupper did not lie down. Its {STOPPED_ON_BOARD_MESSAGE}"}
    return {"success": False, "message": "stopped the ride, but Pupper did not lie down: it is still balancing on"
                                         " the skateboard. To get off, a person holds the harness, then call"
                                         " end_skating with held=true"}


@app.post("/move/velocity")
async def move_velocity(req: MoveVelocity):
    refused = gemini_tools.on_board_refusal(T(), "move")
    if refused:
        if not (req.forward_backward_velocity or req.right_left_velocity or req.turning_velocity or req.duration):
            return (await T().immediate_stop(relax_playing_trick=True)).as_dict()  # the STOP button
        return refused.as_dict()
    # The browser uses right/clockwise-positive; ROS uses left/counter-clockwise-positive.
    res = await behaviors.walk(T(), req.forward_backward_velocity, -req.right_left_velocity, -req.turning_velocity, req.duration)
    return res.as_dict()


@app.post("/move/direction")
async def move_direction(req: MoveDirection):
    refused = gemini_tools.on_board_refusal(T(), "move")
    if refused:
        return refused.as_dict()
    turn = await behaviors.walk(T(), 0.0, 0.0, -90.0 if req.heading > 0 else 90.0, abs(req.heading) / 90.0) if req.heading else None
    if turn and not turn.ok:
        return turn.as_dict()
    return (await behaviors.walk(T(), req.speed, 0.0, 0.0, req.duration)).as_dict()


@app.post("/animation")
async def animation(req: Trick):
    refused = gemini_tools.on_board_refusal(T(), "play_trick")
    return (refused or await behaviors.play_trick(T(), req.animation_name)).as_dict()


@app.post("/interactive_action")
async def interactive_action(req: TrickAction):
    refused = gemini_tools.on_board_refusal(T(), "interactive_action")
    return (refused or await behaviors.interactive_action(T(), req.action)).as_dict()


@app.get("/interactive_actions")
def interactive_actions():
    """Actions of interactive tricks (e.g. shake a paw while sitting): id -> trick and description."""
    return {aid: {"trick": t.name, "description": t.actions[clip]} for aid, (t, clip) in T().action_ids().items()}


@app.get("/animations")
def animations():
    return available_tricks()


def available_tricks() -> dict:
    """The tricks in <repo>/tricks, every one played by a policy."""
    return {
        name: {"description": t.description, "duration_s": round(t.total_time, 1), "kind": t.kind}
        for name, t in T().tricks.items()
    }


@app.post("/queue/wait")
async def queue_wait(req: Wait):
    return (await T().wait(req.duration)).as_dict()


@app.post("/queue/reset")
async def queue_reset():
    return (await T().clear_queue()).as_dict()


@app.post("/queue/stop")
@app.post("/stop/immediate")
async def stop_immediate():
    return (await T().immediate_stop(relax_playing_trick=True)).as_dict()


@app.get("/queue/status")
def queue_status():
    return T().status()


@app.post("/following/activate")
async def following_on():
    return (await T().set_following(True)).as_dict()


@app.post("/following/deactivate")
async def following_off():
    return (await T().set_following(False)).as_dict()


@app.get("/system/mode")
async def system_mode():
    return (await T().mode()).as_dict()


@app.get("/limits")
def limits():
    return LIMITS.__dict__


# ------------------------------------------------------------------ sensing
# What Gemini sees: the fisheye frame undistorted to an equirectangular view, as
# Lab 5 hands the person detector (same lens model, FOV and 700-pixel width), so
# people look like people instead of bending around the edge of the lens.
CAMERA_PARAMS = Path(__file__).resolve().parent / "camera_params.yaml"
EQUIRECT_WIDTH = 700
EQUIRECT_FOV_DEG = 220.0
camera_model = None


def undistort_jpeg(data: bytes) -> bytes | None:
    global camera_model
    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return None
    h, w = frame.shape[:2]
    if camera_model is None or (camera_model.width, camera_model.height) != (w, h):
        camera_model = load_camera_model(CAMERA_PARAMS, w, h)
    equirect = fisheye_to_equirectangular(frame, camera_model, EQUIRECT_WIDTH,
                                          equirect_height(EQUIRECT_WIDTH, EQUIRECT_FOV_DEG),
                                          h_fov_deg=EQUIRECT_FOV_DEG)
    ok, jpeg = cv2.imencode(".jpg", equirect, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return jpeg.tobytes() if ok else None


@app.get("/camera/image")
def camera_image():
    try:
        msg = T().latest_image.get_nowait()
    except Exception:
        return Response(status_code=503, content="no camera frame yet")
    T().latest_image.put_nowait(msg)  # keep it for the next poll
    jpeg = undistort_jpeg(bytes(msg.data))
    if jpeg is None:
        return Response(status_code=503, content="could not decode the camera frame")
    return Response(content=jpeg, media_type="image/jpeg")


@app.websocket("/ws/detections")
async def ws_detections(ws: WebSocket):
    await ws.accept()
    detections.clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        detections.clients.discard(ws)


@app.get("/system/status")
async def system_status():
    status = await asyncio.to_thread(host_status)  # the battery script and network check block
    status["skate"] = await skate_status(tools)
    return status


async def skate_status(robot: Robot | None) -> dict:
    """Skate mode for the status bar: the loaded policy, a config error, the controller's state.

    controller: "active" the skate policy holds the legs; "inactive" it is loaded but not
    running (a loaded but unconfigured or finalized controller reads as inactive too);
    "missing" no skate config, or the controller manager doesn't list it; "unknown" the
    server or the controller manager isn't ready, or asking it failed. The status bar polls
    this, so it never waits for the controller manager to come up.
    """
    cfg = robot.skate if robot else None
    controller = "missing" if robot else "unknown"
    if cfg is not None:
        controller = "unknown"
        try:
            if robot.list_client.service_is_ready():
                state = (await robot.controller_states()).get(SKATE_CONTROLLER)
                controller = "active" if state == "active" else "inactive" if state else "missing"
        except Exception as e:
            log.warning("status: could not read the skate controller's state: %s", e)
    return {"installed": cfg is not None, "policy": cfg.name if cfg else None, "tested": cfg.tested if cfg else None,
            "error": robot.skate_error if robot else None, "controller": controller}


def host_status() -> dict:
    pct = volts = None
    if BATTERY_SCRIPT.exists():
        try:
            out = subprocess.run(["python3", str(BATTERY_SCRIPT)], capture_output=True, text=True, timeout=2).stdout
            m = re.search(r"(\d+(?:\.\d+)?)V", out)
            volts = float(m.group(1)) if m else None
            out = subprocess.run(["python3", str(BATTERY_SCRIPT), "--percentage_only"], capture_output=True, text=True, timeout=2).stdout
            pct = float(out.strip())
        except Exception:
            pass
    temps = psutil.sensors_temperatures()
    cpu_temp = temps["cpu_thermal"][0].current if "cpu_thermal" in temps else None
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=1).close()
        internet = "online"
    except OSError:
        internet = "offline"
    return {
        "battery": {"percentage": pct, "is_charging": False, "voltage": volts},
        "cpu": {"usage": psutil.cpu_percent(), "temperature": cpu_temp},
        "services": {"robot": "active" if tools else "inactive", "internet": internet},
    }


@app.post("/system/volume")
def system_volume(req: Volume):
    level = max(0.0, min(req.volume / 100.0, 1.5))
    r = subprocess.run(["wpctl", "set-volume", "@DEFAULT_SINK@", str(level)], capture_output=True, text=True)
    return {"success": r.returncode == 0, "message": f"volume set to {req.volume}%" if r.returncode == 0 else r.stderr}


@app.get("/health")
def health():
    return {"status": "running", "tools": tools is not None}


# The built web app (npm run build in web/ -> web/dist/) is served at /, so the robot's own
# browser, or a laptop through an SSH tunnel, opens everything from one address.
if WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
