"""
pid_tune.py - critically-damped PID gains for the drift-correction loop.

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

wn is derived from the host's audio block period tau and the bang window: by
default (config.PID_SETTLE_MS == 0) wn is chosen so the critically-damped PID's
P term uses PID_P_EDGE_FRAC of the pitch rail at the window edge, i.e. the
controller stays genuinely linear (unsaturated) all the way across the
+/-window instead of railing the actuator a fraction of a millisecond into it.
Kd is clamped at >= 0: at the window-appropriate wn the model yields a zero
(more precisely a non-positive) Kd -- the plant is too close to a pure
integrator for a useful derivative term at those gains, and the P+I critical
damping is the mathematically right result (a nonzero Kd only appears if wn is
pushed up toward the block rate, where the controller saturates).

Start-up values
---------------
The gains are pure functions of the block period and the bang window, so they
are computed ONCE when the host starts (in the host's __init__, via
compute_gains()) and held in memory for the process lifetime.  No values are
read from or written to disk: there are no per-host PID JSON files.
"""

import math

import config
import msync_common as C


def _default_block_period():
    """Host audio block period (s): 8192 frames @ 44.1 kHz."""
    return 8192.0 / 44100.0


def _wn(block_period, window_sec):
    """Natural frequency wn (rad/s) for the critically-damped gains.

    With config.PID_SETTLE_MS == 0 (default): choose wn so the P term of the
    critically-damped PID uses PID_P_EDGE_FRAC of the pitch rail at the bang-
    window edge, keeping the PID unsaturated across the whole +/-window:

        Kp = 3*wn^2*tau = PID_P_EDGE_FRAC*MAX_PITCH / window_sec
        =>  wn = sqrt(PID_P_EDGE_FRAC*MAX_PITCH / (3*tau*window_sec))

    With config.PID_SETTLE_MS > 0, wn is instead derived from that target
    settle time (~95% settled in settle_s for a triple pole). Either way wn is
    capped to a fraction of the host's block/loop rate so the loop stays well
    below it."""
    tau = max(block_period, 1e-6)
    settle_s = config.PID_SETTLE_MS / 1000.0
    if settle_s > 0.0:
        wn = 3.0 / settle_s
    else:
        w = max(float(window_sec), 1e-6)
        wn = math.sqrt((C.PID_P_EDGE_FRAC * C.MAX_PITCH) / (3.0 * tau * w))
    return float(min(wn, config.PID_OMEGA_MAX_FRAC / tau))


def compute_gains(block_period=None, window_sec=None):
    """Critically-damped PID gains (Kp, Ki, Kd) + model params, computed from
    the host's audio block period and the +-bang window. No persistence: the
    host calls this once at start-up and keeps the result in memory.

    `block_period` (s) defaults to the standard 8192/44.1k block. `window_sec`
    is the +-bang window (seconds); it defaults to
    config.BANG_BANG_WINDOW_MS."""
    tau = max(float(block_period) if block_period else _default_block_period(),
              1e-6)
    if window_sec is None:
        window_sec = config.BANG_BANG_WINDOW_MS / 1000.0
    wn = float(_wn(tau, window_sec))
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
        "window_ms": float(window_sec) * 1000.0,
    }