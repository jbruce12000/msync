"""
pid_tune.py - per-host, autodetected, critically-damped PID gains for the
drift-correction loop, persisted to a per-host JSON file.

Model
-----
Over one audio block the local playhead advances by ~T_block * (1 + pitch), so
the sync error e = target - local_pos evolves (to first order) as

    d(e)/dt = -pitch = -u           =>   P(s) = 1/s

from control (pitch) to error. The block-quantized playhead adds a small lag
tau (chosen as the audio block period), giving the plant

    P(s) = 1 / (s * (1 + tau*s))

A PID controller C(s) = Kp + Ki/s + Kd*s on that plant has closed-loop
characteristic polynomial

    tau*s^3 + (1 + Kd)*s^2 + Kp*s + Ki

Matching to the critically-damped triple pole (s + wn)^3 yields:

    Kp = 3 * wn^2 * tau
    Ki =     wn^3 * tau
    Kd = 3 * wn * tau - 1

wn is derived from a target settling time (config.PID_SETTLE_MS) and the host's
detected audio block period tau. Kd is clamped at >= 0: when the model yields a
negative Kd the plant is too close to a pure integrator for a useful derivative
term at that wn, and the PI critical damping is the mathematically right result.

Persistence
-----------
The first time a host runs an audio callback it detects its block period
(blocksize / sample_rate), computes the gains above, and writes a per-host JSON
file ("pid_client.json" / "pid_server.json", or $MSYNC_PID_FILE). Thereafter the
file is ONLY read and never recomputed: "once calculated, values do not change".
Each host therefore keeps its own, host-specific, permanent gains.
"""

import json
import os
import time

import config
import msync_common as C


def _here_dir():
    """Directory of this file (config-dir for per-host PID values)."""
    return os.path.dirname(os.path.abspath(__file__))


def pid_file_path(tag):
    """Absolute path of this host's PID values file (env-overridable)."""
    env = os.environ.get("MSYNC_PID_FILE")
    if env:
        return env
    return os.path.join(_here_dir(), "pid_%s.json" % tag)


def _default_block_period():
    """Fallback block period (s) when a track's sample rate is not known yet."""
    return 8192.0 / 44100.0


def _wn(block_period):
    """Natural frequency wn (rad/s) from the target settling time, capped to a
    fraction of the host's block/loop rate so the loop stays well below it."""
    settle_s = max(config.PID_SETTLE_MS / 1000.0, 1e-3)
    wn = 3.0 / settle_s                      # ~95% settled in settle_s
    block_rate = 1.0 / max(block_period, 1e-6)
    return min(wn, config.PID_OMEGA_MAX_FRAC * block_rate)


def compute_gains(block_period):
    """Critically-damped PID gains (Kp, Ki, Kd) + model params for a host
    running audio blocks of `block_period` seconds."""
    tau = max(float(block_period), 1e-6)
    wn = float(_wn(tau))
    kp = 3.0 * wn * wn * tau
    ki = wn * wn * wn * tau
    kd = max(0.0, 3.0 * wn * tau - 1.0)
    return {
        "kp": kp,
        "ki": ki,
        "kd": kd,
        "wn": wn,
        "tau": tau,
        "block_period": tau,
    }


def load(tag, block_period=None):
    """Return (gains_dict, path) for this host. If a values file already exists
    return it UNCHANGED; otherwise detect (from block_period), persist it, and
    return the newly calculated values. Never recomputes once a file exists."""
    path = pid_file_path(tag)
    if os.path.isfile(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if {"kp", "ki", "kd"}.issubset(data.keys()):
                return data, path
        except (OSError, ValueError):
            pass
    bp = block_period if block_period else _default_block_period()
    data = compute_gains(bp)
    data["tag"] = tag
    data["detected_at"] = round(time.time(), 3)
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        C.logger().info("per-host PID values detected and written to %s "
                        "(Kp=%.4f Ki=%.4f Kd=%.4f wn=%.3f rad/s tau=%.4fs)",
                        path, data["kp"], data["ki"], data["kd"],
                        data["wn"], data["tau"])
    except OSError as exc:
        C.logger().warning("could not write PID values to %s: %s", path, exc)
    return data, path
