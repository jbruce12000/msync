"""
pid_tune.py - per-host Ziegler-Nichols (reaction-curve) auto-tuning, critically
damped, persisted to a per-host JSON file.

Method
------
Ziegler-Nichols open-loop reaction-curve tuning, adapted to this integrating
plant.  Over one audio block the local playhead advances by ~T_block*(1+pitch),
so the sync error e = target - local_pos evolves (to first order) as

    d(e)/dt = -pitch = -u            =>    P(s) = 1/s

(a unit-gain integrator) with the host's *effective* dead time L in between:
the block-quantized playhead, the err_f EMA the controller actually drives
from, and (on clients) the network/reference path.  On the very first run (no
pid file exists yet) each host therefore measures its own L empirically with
the classic ZN step test (see StepTest):

    1. wait for the smoothed error to flatten (start-up transient, see
       StepTest), running a few baseline blocks with pitch = 0 and recording
    2. apply a fixed +STEP pitch for several blocks, recording further
    3. a two-line fit locates the breakpoint where the error's slope changes:
       that delay is L, and the slope ratio confirms the unit integrator gain
       (K = slope_change / STEP ~ -1)

The gains are then designed for CRITICAL DAMPING on P(s) = 1/(s(1+L*s)) by
matching the closed-loop characteristic polynomial to the triple pole
(s + wn)^3:

    L*s^3 + (1 + Kd)*s^2 + Kp*s + Ki  ==  (s + wn)^3

        Kp = 3*wn^2*L      Ki = wn^3*L      Kd = 3*wn*L - 1

By default wn is chosen so the P term uses PID_P_EDGE_FRAC of the pitch rail at
the bang-window edge (Kp = PID_P_EDGE_FRAC*MAX_PITCH/window), keeping the PID
genuinely linear (unsaturated) all the way across the +/-window.  A larger
measured L (slower host path) then yields a smaller wn and a gentler, still
critically-damped, loop -- exactly the per-host behavior the step test is for.
(Classic ZN's own Ti = 2*L ratio would make the integral term far too hot for
an integrating plant at the window-anchored P gain -- it rings and re-rails
the actuator -- so the ZN *measurement* feeds a critically-damped *design*
instead.)  Kd is clamped at >= 0: at the window-appropriate wn it is zero --
the plant is too close to a pure integrator for a useful derivative term, and
P+I critical damping is the mathematically right result.

Persistence
-----------
The first time a host runs an audio callback it measures L (or falls back to
the block period), computes the gains above, and writes a per-host JSON file
("pid_client.json" / "pid_server.json", or $MSYNC_PID_FILE) including the
measured lag.  Thereafter the file is ONLY read and never recomputed: "once
calculated, values do not change".  Each host therefore keeps its own,
host-specific, permanent gains -- including its own measured dead time.
"""

import json
import math
import os
import time

import numpy as np

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


def _design_lag(block_period, lag_sec):
    """Effective dead time L (s) for the design: the measured reaction-curve
    lag when available, else the host's audio block period."""
    if lag_sec is not None and float(lag_sec) > 1e-6:
        return max(float(lag_sec), 1e-6)
    return max(float(block_period), 1e-6)


def _wn(block_period, window_sec, lag_sec=None):
    """Natural frequency wn (rad/s) for the critically-damped gains.

    With config.PID_SETTLE_MS == 0 (default): choose wn so the P term of the
    critically-damped PID uses PID_P_EDGE_FRAC of the pitch rail at the bang-
    window edge, keeping the PID unsaturated across the whole +/-window:

        Kp = 3*wn^2*L = PID_P_EDGE_FRAC*MAX_PITCH / window_sec
        =>  wn = sqrt(PID_P_EDGE_FRAC*MAX_PITCH / (3*L*window_sec))

    where L is the host's measured (or block-period) lag -- a slower host path
    therefore gets a gentler loop.  With config.PID_SETTLE_MS > 0, wn is
    instead derived from that target settle time (~95% settled in settle_s for
    a triple pole).  Either way wn is capped to a fraction of the host's
    block/loop rate so the loop stays well below it."""
    tau = _design_lag(block_period, lag_sec)
    settle_s = config.PID_SETTLE_MS / 1000.0
    if settle_s > 0.0:
        wn = 3.0 / settle_s
    else:
        w = max(float(window_sec), 1e-6)
        wn = math.sqrt((C.PID_P_EDGE_FRAC * C.MAX_PITCH) / (3.0 * tau * w))
    return float(min(wn, config.PID_OMEGA_MAX_FRAC / tau))


def compute_gains(block_period, window_sec=None, lag_sec=None):
    """Critically-damped PID gains (Kp, Ki, Kd) + model params for a host.

    `block_period` is the host's audio block period (s).  `window_sec` is the
    +-bang window (seconds); it defaults to config.BANG_BANG_WINDOW_MS.
    `lag_sec`, when provided, is the host's empirically measured dead time
    (ZN reaction-curve step test); the design then uses it instead of the
    block period, so a host with extra loop lag gets a gentler, still
    critically-damped, controller.
    """
    bp = max(float(block_period), 1e-6)
    if window_sec is None:
        window_sec = config.BANG_BANG_WINDOW_MS / 1000.0
    tau = _design_lag(bp, lag_sec)
    wn = float(_wn(bp, window_sec, lag_sec))
    kp = 3.0 * wn * wn * tau
    ki = wn * wn * wn * tau
    kd = max(0.0, 3.0 * wn * tau - 1.0)
    return {
        "kp": kp,
        "ki": ki,
        "kd": kd,
        "wn": wn,
        "tau": bp,
        "block_period": bp,
        "window_ms": float(window_sec) * 1000.0,
        "lag_ms": float(tau) * 1000.0,
        "method": "zn-reaction-curve" if lag_sec is not None else "zn-fallback",
    }


def fit_lag(tb, yb, ts, ys, step_frac, lag_max=1.5):
    """Two-line fit for the ZN reaction-curve breakpoint.

    tb/yb: baseline (t, smoothed-error) samples recorded before the step.
    ts/ys: step-phase samples recorded after the step was commanded.
    step_frac: the applied pitch step, as a fraction (e.g. 600e-6).
    Return (lag, K): lag = delay from the step phase start to the error's
    slope breakpoint (s); K = process gain = (post-slope - pre-slope)/step
    (~ -1.0 for the unit-gain integrator: a +pitch step makes the error fall
    faster).  Both None if no usable fit exists.
    """
    X = np.concatenate([np.asarray(tb, float), np.asarray(ts, float)])
    Y = np.concatenate([np.asarray(yb, float), np.asarray(ys, float)])
    if len(X) < 8:
        return None, None
    t0 = float(ts[0])
    dt_est = float(ts[1] - ts[0]) if len(ts) > 1 else 0.1
    bmin = t0 + 0.02
    bmax = t0 + float(max(0.02, min(lag_max, X[-1] - t0)))
    best = None
    for B in np.arange(bmin, bmax + 1e-12, max(dt_est * 0.2, 1e-3)):
        pre, post = X < B, X >= B
        if pre.sum() < 3 or post.sum() < 3:
            continue
        m1, c1 = np.polyfit(X[pre], Y[pre], 1)
        m2, c2 = np.polyfit(X[post], Y[post], 1)
        res = float(np.sum((Y[pre] - (m1 * X[pre] + c1)) ** 2)
                    + np.sum((Y[post] - (m2 * X[post] + c2)) ** 2))
        if best is None or res < best[0]:
            best = (res, float(B), float(m1), float(m2))
    if best is None:
        return None, None
    _, B, m1, m2 = best
    lag = float(B - t0)
    K = float((m2 - m1) / step_frac)
    return lag, K


class StepTest:
    """Ziegler-Nichols reaction-curve step test, driven one block per feed().

    The host calls feed(err, dt) from its audio callback for every block while
    no PID file exists (first run, PID test mode).  Phases:

      settle   -- pitch 0; wait until the smoothed error is flat (its slope
                  over a short window is below PID_CAL_SETTLE_SLOPE x step),
                  or PID_CAL_SETTLE_BLOCKS blocks elapse.  Skipped entirely
                  when PID_CAL_SETTLE_BLOCKS is 0.  This keeps the playback
                  start-up transient (err_f converging toward the initial
                  offset, slope ~ms/s) from swamping the much smaller step
                  response that follows.
      baseline -- pitch 0; record smoothed error for PID_CAL_BASELINE_BLOCKS.
      step     -- apply a fixed +PID_STEP_PPM pitch for PID_CAL_STEP_BLOCKS
                  blocks while recording.

    On the final step block a two-line fit (fit_lag) locates the breakpoint
    where the error's slope changes: that delay is the host's effective loop
    dead time L, with process gain K = slope_change/step (~ -1.0).  The host
    then computes and persists the critically-damped gains from L (see
    load(... lag_sec=...)).

    Host-facing state after a feed(): `.pitch` (pitch fraction to apply THIS
    block), `.phase`, `.done` (True after the final step block), `.lag`, `.K`
    and `.lag_ok` (whether the measurement is usable; when False the host
    should fall back to the analytic block-period lag).  feed() returns True
    on the finishing call.
    """
    def __init__(self, step_ppm=None):
        self.step_ppm = float(step_ppm if step_ppm is not None
                              else config.PID_STEP_PPM)
        self.step_frac = self.step_ppm * 1e-6
        self.n_baseline = int(config.PID_CAL_BASELINE_BLOCKS)
        self.n_step = int(config.PID_CAL_STEP_BLOCKS)
        self.n_settle = int(config.PID_CAL_SETTLE_BLOCKS)
        self.settle_slope_frac = float(config.PID_CAL_SETTLE_SLOPE)
        self.phase = "settle" if self.n_settle > 0 else "baseline"
        self.pitch = 0.0
        self.done = False
        self.lag = None
        self.K = None
        self.lag_ok = False
        self._t = 0.0
        self._settle_count = 0
        self._ring_t = []
        self._ring_y = []
        self._tb = []
        self._yb = []
        self._ts = []
        self._ys = []

    def feed(self, err, dt):
        if self.done:
            return True
        self._t += float(dt)
        if self.phase == "settle":
            self.pitch = 0.0
            self._settle_count += 1
            self._ring_t.append(self._t)
            self._ring_y.append(float(err))
            if len(self._ring_t) > 5:
                del self._ring_t[0]
                del self._ring_y[0]
            flat = False
            if len(self._ring_t) >= 4:
                span = max(self._ring_t[-1] - self._ring_t[0], 1e-9)
                slope = abs((self._ring_y[-1] - self._ring_y[0]) / span)
                flat = slope <= self.settle_slope_frac * self.step_frac
            if flat or self._settle_count >= self.n_settle:
                self.phase = "baseline"
            return False
        if self.phase == "baseline":
            self._tb.append(self._t)
            self._yb.append(float(err))
            if len(self._tb) >= self.n_baseline:
                self.phase = "step"
                self.pitch = self.step_frac
            else:
                self.pitch = 0.0
        else:
            self._ts.append(self._t)
            self._ys.append(float(err))
            if len(self._ts) >= self.n_step:
                self._finish()
                self.done = True
                self.pitch = 0.0
                return True
            self.pitch = self.step_frac
        return False

    def _finish(self):
        lag, K = fit_lag(self._tb, self._yb, self._ts, self._ys,
                         self.step_frac, config.PID_CAL_LAG_MAX_S)
        ok = (lag is not None and K is not None
              and 0.02 <= lag <= config.PID_CAL_LAG_MAX_S
              and -2.5 <= K <= -0.4)      # slope broke DOWN by ~the step
        self.lag = float(lag) if ok else None
        self.K = float(K) if K is not None else None
        self.lag_ok = ok


def load(tag, block_period=None, window_sec=None, lag_sec=None):
    """Return (gains_dict, path) for this host.  If a values file already
    exists return it UNCHANGED; otherwise detect (from block_period +
    window_sec + measured lag_sec), persist it, and return the newly
    calculated values.  Never recomputes once a file exists."""
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
    data = compute_gains(bp, window_sec, lag_sec)
    data["tag"] = tag
    data["detected_at"] = round(time.time(), 3)
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        C.logger().info(
            "per-host PID values detected and written to %s "
            "(Kp=%.4f Ki=%.4f Kd=%.4f wn=%.3f rad/s lag=%.0fms window=%.0fms%s)",
            path, data["kp"], data["ki"], data["kd"],
            data["wn"], data["lag_ms"], data["window_ms"],
            " [measured]" if data["method"] == "zn-reaction-curve" else
            " [block-period fallback]")
    except OSError as exc:
        C.logger().warning("could not write PID values to %s: %s", path, exc)
    return data, path