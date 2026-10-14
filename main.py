import os
import sys
import time
import math
import warnings
from collections import deque

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
os.environ["GLOG_minloglevel"] = "2"
warnings.filterwarnings("ignore")

import ctypes

try:
    import cv2
    import mediapipe as mp
    import numpy as np
    import pyautogui
except ModuleNotFoundError:
    import subprocess
    script_dir = os.path.dirname(os.path.abspath(__file__))
    venv_py = os.path.join(script_dir, "venv", "Scripts", "python.exe")
    if os.path.exists(venv_py) and os.path.abspath(sys.executable).lower() != os.path.abspath(venv_py).lower():
        print(f"[AeroClick] Switching to project environment: {venv_py}")
        try:
            sys.exit(subprocess.call([venv_py, os.path.abspath(__file__)] + sys.argv[1:]))
        except KeyboardInterrupt:
            sys.exit(0)  # user pressed Ctrl+C — exit silently, app already cleaned up
    else:
        try:
            print("[AeroClick] Switching to Python 3.12 runtime...")
            sys.exit(subprocess.call(["py", "-3.12", os.path.abspath(__file__)] + sys.argv[1:]))
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception:
            print("Error: Missing required packages. Please install requirements.txt.")
            raise

# ── Windows DPI awareness ─────────────────────────────────────────────────────
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

pyautogui.PAUSE = 0
pyautogui.FAILSAFE = False

WINDOW_TITLE = "AeroClick - Touchless Mouse Control"

# ==============================================================================
# 1-EURO FILTER  (Casiez, Roussel & Vogel — CHI 2012)
# Reference: "1€ Filter: A Simple Speed-based Low-pass Filter for Noisy Input
#             in Interactive Systems."  Proc. CHI 2012, pp. 2527–2530.
#
# Solves the jitter-vs-lag tradeoff by adapting the filter cutoff to hand speed:
#   hand still   →  low cutoff  → heavy jitter suppression
#   hand moving  →  high cutoff → minimal lag
# Tune: set beta=0, lower min_cutoff until jitter disappears at rest,
#       then raise beta until lag disappears during fast sweeps.
# ==============================================================================
class OneEuroFilter:
    def __init__(self, freq=30.0, min_cutoff=0.9, beta=0.005, d_cutoff=1.0):
        self.freq = freq
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x = None
        self._dx = 0.0
        self._last_ts = None

    @staticmethod
    def _alpha(cutoff, freq):
        te = 1.0 / freq
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / te)

    def reset(self):
        self._x = None
        self._dx = 0.0
        self._last_ts = None

    def __call__(self, x, ts=None):
        if ts is not None and self._last_ts is not None:
            dt = ts - self._last_ts
            if dt > 0:
                self.freq = 1.0 / dt
        if ts is not None:
            self._last_ts = ts
        if self._x is None:
            self._x = x
            return x
        dx = (x - self._x) * self.freq
        a_d = self._alpha(self.d_cutoff, self.freq)
        self._dx = a_d * dx + (1.0 - a_d) * self._dx
        cutoff = self.min_cutoff + self.beta * abs(self._dx)
        a = self._alpha(cutoff, self.freq)
        self._x = a * x + (1.0 - a) * self._x
        return self._x


# ==============================================================================
# VELOCITY-ADAPTIVE C-D GAIN  (Fitts\'s Law principle)
# Reference: Fitts, P. M. (1954). "The information capacity of the human motor
#            system in controlling the amplitude of movement." J. Exp. Psych.
#
# Fast hand transit phase → amplify more so the cursor crosses the screen
# with less physical effort, reducing "gorilla arm" fatigue.
# Slow precision phase → lower gain, finger-tip-level accuracy.
# ==============================================================================
class AdaptiveCDGain:
    def __init__(self, base=1.0, peak=2.0, threshold=18.0):
        self.base = base
        self.peak = peak
        self.threshold = threshold
        self._px = self._py = None

    def reset(self):
        self._px = self._py = None

    def get(self, rx, ry):
        if self._px is None:
            self._px, self._py = rx, ry
            return self.base
        spd = math.hypot(rx - self._px, ry - self._py)
        self._px, self._py = rx, ry
        t = min(1.0, spd / self.threshold)
        return self.base + t * (self.peak - self.base)


# ==============================================================================
# HAND VELOCITY TRACKER  — gesture action gate
# Reference: Wobbrock, J.O. et al. UIST 2007 ($1 recognizer, motion-gate idea)
# Fast hand movement = user is in transit, not trying to trigger a gesture.
# Raise confirmation thresholds during fast motion to reduce accidental clicks.
# ==============================================================================
class VelocityTracker:
    def __init__(self, window=5):
        self._buf = deque(maxlen=window)
        self._px = self._py = None

    def update(self, x, y):
        if self._px is None:
            self._px, self._py = x, y
            return 0.0
        spd = math.hypot(x - self._px, y - self._py)
        self._px, self._py = x, y
        self._buf.append(spd)
        return self.avg

    def reset(self):
        self._buf.clear()
        self._px = self._py = None

    @property
    def avg(self):
        return sum(self._buf) / len(self._buf) if self._buf else 0.0


# ==============================================================================
# WEIGHTED GESTURE VOTE  — improved debouncing
# Reference: Kela, J. et al. (2006). "Accelerometer-based gesture control
#            for a design environment." Personal and Ubiquitous Computing 10(5).
# Recent frames weighted more heavily: faster response to clear gestures,
# robust against transient mis-classifications.
# ==============================================================================
def weighted_vote(history):
    if not history:
        return "NO HAND"
    n = len(history)
    scores = {}
    for i, g in enumerate(history):
        w = (i + 1) / n
        scores[g] = scores.get(g, 0.0) + w
    return max(scores, key=scores.get)


def gesture_conf(history, gesture):
    if not history:
        return 0.0
    return sum(1 for g in history if g == gesture) / len(history)


# ==============================================================================
# CONFIGURATION
# ==============================================================================
GESTURE_HISTORY_LEN     = 8

# 1€ Filter — tuned for 30 fps MediaPipe hand tracking
FILTER_MIN_CUTOFF       = 0.9
FILTER_BETA             = 0.005
FILTER_D_CUTOFF         = 1.0

# Velocity gate
VELOCITY_GATE_THR       = 12.0   # px/frame → extra confirmation required
VELOCITY_EXTRA_CONFIRM  = 2

MARGIN_X, MARGIN_Y      = 90, 70

PINCH_COOLDOWN          = 0.45
MIN_PINCH_CONFIRM       = 2
PINCH_DIST_THR          = 0.35
MIN_PINCH_RELEASE       = 3

DRAG_ARM_TIMEOUT        = 2.0
MIN_FIST_CONFIRM        = 2
MIN_FIST_RELEASE        = 4

RC_COOLDOWN             = 0.55
MIN_RC_CONFIRM          = 4

DC_COOLDOWN             = 0.55
MIN_DC_CONFIRM          = 4

FF_COOLDOWN             = 0.8
MIN_FF_CONFIRM          = 3
MIN_FF_RELEASE          = 3

MIN_NEUTRAL_CONFIRM     = 3
ACTION_LOCK_MIN         = 0.35

SCROLL_TRIGGER_DIST     = 45
SCROLL_DEADZONE         = 18
SCROLL_COOLDOWN         = 0.35
SCROLL_BASE             = 120
SCROLL_MAX              = 300
REVERSAL_DELAY          = 0.60

HUD_ACTION_DURATION     = 0.9
ONBOARDING_SECS         = 12.0

# Sensitivity presets — selectable with +/- keys
PRESETS = [
    {"name": "Low",    "min_cutoff": 0.5,  "beta": 0.003, "cd_peak": 1.6},
    {"name": "Medium", "min_cutoff": 0.9,  "beta": 0.005, "cd_peak": 2.0},
    {"name": "High",   "min_cutoff": 1.6,  "beta": 0.010, "cd_peak": 2.5},
]
DEFAULT_PRESET = 1

# Colours (BGR)
GESTURE_COL = {
    "POINT":        (0,   230, 255),
    "PINCH":        (255, 105, 180),
    "RIGHT CLICK":  (255, 80,  255),
    "DOUBLE CLICK": (255, 230, 0),
    "FOUR FINGERS": (255, 180, 0),
    "OPEN PALM":    (60,  230, 80),
    "FIST":         (50,  130, 255),
    "WAITING...":   (160, 160, 160),
    "NO HAND":      (110, 110, 110),
}
STATUS_COL = {
    "ACTIVE":       (60,  230, 80),
    "PAUSED":       (0,   215, 255),
    "DRAGGING":     (0,   165, 255),
    "ARMED":        (0,   200, 255),
    "BROWSER BACK": (255, 180, 0),
    "BLOCKED":      (60,  80,  240),
    "IDLE":         (160, 160, 160),
}
ACTION_COL = {
    "LEFT CLICK":   (255, 105, 180),
    "RIGHT CLICK":  (255, 80,  255),
    "DOUBLE CLICK": (255, 230, 0),
    "BROWSER BACK": (255, 180, 0),
    "DRAG START":   (0,   165, 255),
    "DRAG END":     (60,  230, 80),
    "SCROLL UP":    (0,   240, 255),
    "SCROLL DOWN":  (0,   240, 255),
    "NONE":         (140, 140, 140),
}
ACCENT  = (200, 160, 255)
DIM     = (120, 120, 120)
WHITE   = (225, 225, 225)
BORDER  = (55,  55,  55)
DARKBG  = (18,  18,  18)

LEGEND = [
    ("POINT",       "Move & Scroll",   GESTURE_COL["POINT"]),
    ("PINCH",       "Left Click",      GESTURE_COL["PINCH"]),
    ("2 FINGERS",   "Right Click",     GESTURE_COL["RIGHT CLICK"]),
    ("3 FINGERS",   "Double Click",    GESTURE_COL["DOUBLE CLICK"]),
    ("4 FINGERS",   "Browser Back",    GESTURE_COL["FOUR FINGERS"]),
    ("PINCH+FIST",  "Drag & Drop",     (0, 165, 255)),
    ("OPEN PALM",   "Pause",           GESTURE_COL["OPEN PALM"]),
    ("FIST",        "Idle",            GESTURE_COL["FIST"]),
]


# ==============================================================================
# HELPERS
# ==============================================================================
def glass_panel(img, p1, p2, bg=DARKBG, bc=BORDER, a=0.78):
    ov = img.copy()
    cv2.rectangle(ov, p1, p2, bg, -1)
    cv2.addWeighted(ov, a, img, 1 - a, 0, img)
    cv2.rectangle(img, p1, p2, bc, 1, cv2.LINE_AA)


def conf_bar(img, x, y, w, val, col=(60, 200, 80)):
    bw = int(w * max(0.0, min(1.0, val)))
    cv2.rectangle(img, (x, y), (x + w,  y + 4), (35, 35, 35), -1)
    cv2.rectangle(img, (x, y), (x + bw, y + 4), col,          -1)


def calc_dist(p1, p2):
    x1, y1 = (p1.x, p1.y) if hasattr(p1, "x") else p1
    x2, y2 = (p2.x, p2.y) if hasattr(p2, "x") else p2
    return math.hypot(x1 - x2, y1 - y2)


def finger_up(lm, tip, pip, wrist=0):
    """Tip farther from wrist than PIP = finger extended (tilt-invariant)."""
    return calc_dist(lm[tip], lm[wrist]) > calc_dist(lm[pip], lm[wrist])


def detect_gesture(lm):
    palm = calc_dist(lm[0], lm[9])
    if palm == 0:
        return "UNKNOWN"
    idx = finger_up(lm, 8,  6)
    mid = finger_up(lm, 12, 10)
    rng = finger_up(lm, 16, 14)
    pky = finger_up(lm, 20, 18)
    thm = calc_dist(lm[4], lm[17]) > calc_dist(lm[2], lm[17])
    pnch = calc_dist(lm[4], lm[8]) / palm
    cnt = idx + mid + rng + pky
    if cnt == 0:                                         return "FIST"
    if cnt == 4 and thm:                                 return "OPEN PALM"
    if cnt == 4 and not thm:                             return "FOUR FINGERS"
    if idx and mid and rng and not pky:                  return "DOUBLE CLICK"
    if idx and mid and not rng and not pky:              return "RIGHT CLICK"
    if pnch < PINCH_DIST_THR and not mid and not rng and not pky: return "PINCH"
    if idx and not mid and not rng and not pky:          return "POINT"
    return "WAITING..."


def over_app(cx, cy):
    try:
        r = cv2.getWindowImageRect(WINDOW_TITLE)
        if r and len(r) == 4:
            wx, wy, ww, wh = r
            return (wx-10 <= cx <= wx+ww+10) and (wy-45 <= cy <= wy+wh+10)
    except Exception:
        pass
    return False


# ==============================================================================
# ONBOARDING OVERLAY  (shown for first ONBOARDING_SECS seconds)
# ==============================================================================
def draw_onboarding(frame, remaining):
    h, w = frame.shape[:2]
    alpha = min(1.0, remaining / 2.0)   # fade out in last 2 s

    ov = frame.copy()
    cv2.rectangle(ov, (0, 0), (w, h), (8, 8, 18), -1)
    cv2.addWeighted(ov, 0.52 * alpha, frame, 1 - 0.52 * alpha, 0, frame)

    cw, ch = 460, 370
    cx1, cy1 = w//2 - cw//2, h//2 - ch//2
    cx2, cy2 = cx1 + cw, cy1 + ch

    ov2 = frame.copy()
    cv2.rectangle(ov2, (cx1, cy1), (cx2, cy2), (18, 16, 32), -1)
    cv2.addWeighted(ov2, 0.90 * alpha, frame, 1 - 0.90 * alpha, 0, frame)
    cv2.rectangle(frame, (cx1, cy1), (cx2, cy2), (90, 60, 150), 2, cv2.LINE_AA)

    # Title
    title = "AeroClick  -  Gesture Guide"
    tw = cv2.getTextSize(title, cv2.FONT_HERSHEY_DUPLEX, 0.70, 1)[0][0]
    col_a = tuple(int(c * alpha) for c in ACCENT)
    cv2.putText(frame, title, (w//2 - tw//2, cy1 + 34),
                cv2.FONT_HERSHEY_DUPLEX, 0.70, col_a, 1, cv2.LINE_AA)

    sub = "Keep your right hand visible inside the dashed box"
    sw = cv2.getTextSize(sub, cv2.FONT_HERSHEY_SIMPLEX, 0.38, 1)[0][0]
    cv2.putText(frame, sub, (w//2 - sw//2, cy1 + 56),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38,
                tuple(int(c * alpha) for c in DIM), 1, cv2.LINE_AA)
    cv2.line(frame, (cx1+16, cy1+64), (cx2-16, cy1+64), (55, 40, 80), 1, cv2.LINE_AA)

    # Two-column legend
    col_w = cw // 2 - 10
    entry_h = 36
    entries = LEGEND
    ey = cy1 + 82
    for i, (gname, gdesc, gcol) in enumerate(entries):
        col_off = (i % 2) * (col_w + 16)
        ex = cx1 + 16 + col_off
        box_col = tuple(int(c * 0.35) for c in gcol)
        cv2.rectangle(frame, (ex, ey - 14), (ex + col_w, ey + 16), box_col, -1)
        cv2.rectangle(frame, (ex, ey - 14), (ex + col_w, ey + 16),
                      tuple(int(c * alpha) for c in gcol), 1, cv2.LINE_AA)
        cv2.putText(frame, gname,
                    (ex + 6, ey + 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38,
                    tuple(int(c * alpha) for c in gcol), 1, cv2.LINE_AA)
        cv2.putText(frame, gdesc,
                    (ex + 6, ey + 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.32,
                    tuple(int(c * alpha * 0.75) for c in WHITE), 1, cv2.LINE_AA)
        if i % 2 == 1:
            ey += entry_h + 4

    # Countdown arc
    rc, ry, rr = cx2 - 30, cy1 + 30, 20
    prog = max(0.0, remaining / ONBOARDING_SECS)
    ang = int(360 * (1.0 - prog))
    cv2.circle(frame, (rc, ry), rr, (40, 30, 60), 2, cv2.LINE_AA)
    if ang < 360:
        cv2.ellipse(frame, (rc, ry), (rr, rr), -90, 0, ang, col_a, 2, cv2.LINE_AA)
    stxt = str(int(math.ceil(remaining)))
    stw = cv2.getTextSize(stxt, cv2.FONT_HERSHEY_SIMPLEX, 0.44, 1)[0][0]
    cv2.putText(frame, stxt, (rc - stw//2, ry + 6),
                cv2.FONT_HERSHEY_SIMPLEX, 0.44, col_a, 1, cv2.LINE_AA)

    skip = "Press SPACE to skip"
    skw = cv2.getTextSize(skip, cv2.FONT_HERSHEY_SIMPLEX, 0.36, 1)[0][0]
    cv2.putText(frame, skip, (w//2 - skw//2, cy2 - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.36,
                tuple(int(c * alpha) for c in DIM), 1, cv2.LINE_AA)


# ==============================================================================
# HUD
# ==============================================================================
def draw_hud(frame, tracking, gesture, status, action,
             fps, sens_name, conf, speed, state):
    h, w = frame.shape[:2]

    # Left status card
    glass_panel(frame, (10, 10), (262, 165))
    cv2.putText(frame, "AEROCLICK", (20, 35),
                cv2.FONT_HERSHEY_DUPLEX, 0.68, ACCENT, 1, cv2.LINE_AA)
    cv2.putText(frame, f"[{sens_name}]", (155, 35),
                cv2.FONT_HERSHEY_SIMPLEX, 0.33, DIM, 1, cv2.LINE_AA)
    cv2.line(frame, (18, 42), (254, 42), BORDER, 1, cv2.LINE_AA)

    rows = [
        ("Tracking", tracking,
         (60,230,80) if tracking == "ACTIVE" else (100,100,210)),
        ("Gesture",  gesture,  GESTURE_COL.get(gesture, WHITE)),
        ("Status",   status,   STATUS_COL.get(status,   WHITE)),
        ("Action",   action,   ACTION_COL.get(action,   DIM)),
    ]
    ry = 62
    for lbl, val, col in rows:
        cv2.putText(frame, f"{lbl:<8}:", (20, ry),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, DIM, 1, cv2.LINE_AA)
        thick = 2 if val not in ("NONE","NO HAND","IDLE") else 1
        cv2.putText(frame, val, (108, ry),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.44, col, thick, cv2.LINE_AA)
        ry += 22

    cv2.putText(frame, "Conf", (20, ry + 4),
                cv2.FONT_HERSHEY_SIMPLEX, 0.33, DIM, 1, cv2.LINE_AA)
    conf_bar(frame, 60, ry, 184, conf, GESTURE_COL.get(gesture, (60,200,80)))

    # Top-right info strip
    xa = w - 8
    for txt in [f"FPS {fps:4.0f}", f"Spd {speed:4.1f}", f"[{state}]"]:
        tw = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.37, 1)[0][0]
        col = (0,165,255) if "DRAG" in txt else DIM
        cv2.putText(frame, txt, (xa - tw, rows[0][0] and 24 or 0),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.37, col, 1, cv2.LINE_AA)
        xa -= tw + 6  # horizontal strip — place right-aligned
    # redo properly as vertical strip
    ys = [22, 38, 54]
    txts = [f"FPS {fps:.0f}", f"spd {speed:.1f}", f"[{state}]"]
    for y_off, t in zip(ys, txts):
        tw2 = cv2.getTextSize(t, cv2.FONT_HERSHEY_SIMPLEX, 0.37, 1)[0][0]
        sc = (0,165,255) if state == "DRAGGING" else \
             (0,200,255) if state == "DRAG_ARMED" else DIM
        cv2.putText(frame, t, (w - tw2 - 8, y_off),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.37, sc, 1, cv2.LINE_AA)

    # Bottom-right gesture legend
    lw, lh = 196, 148
    lx, ly = w - lw - 8, h - lh - 8
    glass_panel(frame, (lx, ly), (w-8, h-8))
    cv2.putText(frame, "GESTURES", (lx+10, ly+16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.36, DIM, 1, cv2.LINE_AA)
    cv2.line(frame, (lx+8, ly+21), (w-10, ly+21), BORDER, 1, cv2.LINE_AA)
    ey2 = ly + 35
    for gn, gd, gc in LEGEND:
        cv2.putText(frame, gn[:11], (lx+10, ey2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.32, gc, 1, cv2.LINE_AA)
        cv2.putText(frame, gd, (lx+105, ey2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.29,
                    tuple(int(c*0.72) for c in WHITE), 1, cv2.LINE_AA)
        ey2 += 13

    # Dashed interaction zone
    bx1, by1, bx2, by2 = MARGIN_X, MARGIN_Y, w-MARGIN_X, h-MARGIN_Y
    zc = (65, 55, 90)
    d, g = 11, 5
    for x in range(bx1, bx2, d+g):
        cv2.line(frame,(x,by1),(min(x+d,bx2),by1),zc,1)
        cv2.line(frame,(x,by2),(min(x+d,bx2),by2),zc,1)
    for y in range(by1, by2, d+g):
        cv2.line(frame,(bx1,y),(bx1,min(y+d,by2)),zc,1)
        cv2.line(frame,(bx2,y),(bx2,min(y+d,by2)),zc,1)

    # Footer
    footer = "q = quit   |   +/- = sensitivity   |   SPACE = skip tutorial"
    fw = cv2.getTextSize(footer, cv2.FONT_HERSHEY_SIMPLEX, 0.35, 1)[0][0]
    cv2.putText(frame, footer, (w//2 - fw//2, h-6),
                cv2.FONT_HERSHEY_SIMPLEX, 0.35, (70,70,70), 1, cv2.LINE_AA)


# ==============================================================================
# MAIN
# ==============================================================================
def main():
    mp_hands = mp.solutions.hands
    mp_drawing = mp.solutions.drawing_utils
    mp_drawing_styles = mp.solutions.drawing_styles

    hands = mp_hands.Hands(
        static_image_mode=False,
        max_num_hands=1,
        min_detection_confidence=0.70,
        min_tracking_confidence=0.55,
    )

    try:
        screen_w, screen_h = pyautogui.size()
    except Exception:
        screen_w, screen_h = 1920, 1080

    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print("Error: Could not open webcam.")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FPS, 30)

    for _ in range(15):
        ret, frame = cap.read()
        if ret and frame is not None:
            break
        time.sleep(0.05)
    else:
        print("Error: Webcam failed to deliver frames.")
        cap.release()
        return

    cv2.namedWindow(WINDOW_TITLE, cv2.WINDOW_AUTOSIZE)
    try:
        cv2.setWindowProperty(WINDOW_TITLE, cv2.WND_PROP_TOPMOST, 1)
    except Exception:
        pass

    # Sensitivity / filters
    sidx   = DEFAULT_PRESET
    preset = PRESETS[sidx]

    fx = OneEuroFilter(30.0, preset["min_cutoff"], preset["beta"], FILTER_D_CUTOFF)
    fy = OneEuroFilter(30.0, preset["min_cutoff"], preset["beta"], FILTER_D_CUTOFF)
    cd = AdaptiveCDGain(base=1.0, peak=preset["cd_peak"], threshold=18.0)
    drag_cd = AdaptiveCDGain(base=1.0, peak=preset["cd_peak"], threshold=18.0)
    vel = VelocityTracker(5)

    hist = deque(maxlen=GESTURE_HISTORY_LEN)

    pmx = pmy = None   # previous smoothed mouse position

    app_state   = "NORMAL"
    pinch_rel   = True;    pinch_cool  = 0.0
    pinch_cf    = 0;       pinch_rf    = 0
    drag_at     = 0.0
    fist_cf     = 0;       fist_rf     = 0

    alock       = None;    alock_t     = 0.0
    neut_cf     = 0

    rc_rel      = True;    rc_cool     = 0.0;   rc_cf = 0
    dc_rel      = True;    dc_cool     = 0.0;   dc_cf = 0
    ff_rel      = True;    ff_cool     = 0.0;   ff_cf = 0;  ff_rf = 0

    sc_start    = None;    sc_cool     = 0.0
    sc_t        = 0.0;     sc_dir_lbl  = None
    sc_dir      = None;    sc_dir_t    = 0.0;   sc_dz = 0

    hud_act     = "NONE";  hud_exp     = 0.0

    frame_buf: deque = deque(maxlen=30)
    last_ft = time.perf_counter()

    ob_start = time.time()
    ob_active = True

    failures = 0
    MAX_FAIL = 30

    print(f"AeroClick running | Sensitivity: {preset['name']} | Screen {screen_w}x{screen_h}")
    print("q=quit  |  +/-=sensitivity  |  SPACE=skip tutorial")

    try:
        while True:
            ret, frame = cap.read()
            if not ret or frame is None:
                failures += 1
                if failures >= MAX_FAIL:
                    print("Error: Webcam lost. Exiting.")
                    break
                time.sleep(0.04)
                continue
            failures = 0

            t_perf = time.perf_counter()
            frame_buf.append(t_perf - last_ft)
            last_ft = t_perf
            fps = 1.0 / (sum(frame_buf) / len(frame_buf)) if frame_buf else 30.0

            frame = cv2.flip(frame, 1)
            fh, fw, _ = frame.shape
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            res = hands.process(rgb)

            raw   = "NO HAND"
            itip  = None
            dtip  = None
            spd   = vel.avg
            now   = time.time()
            ts    = time.perf_counter()

            if res.multi_hand_landmarks:
                for hlm in res.multi_hand_landmarks:
                    mp_drawing.draw_landmarks(
                        frame, hlm, mp_hands.HAND_CONNECTIONS,
                        mp_drawing_styles.get_default_hand_landmarks_style(),
                        mp_drawing_styles.get_default_hand_connections_style(),
                    )
                    raw  = detect_gesture(hlm.landmark)
                    t8   = hlm.landmark[8]
                    itip = (int(t8.x * fw), int(t8.y * fh))
                    m9   = hlm.landmark[9]
                    dtip = (int(m9.x * fw), int(m9.y * fh))
                    spd  = vel.update(t8.x * fw, t8.y * fh)
            else:
                hist.clear(); fx.reset(); fy.reset()
                cd.reset(); drag_cd.reset(); vel.reset()
                pmx = pmy = None
                sc_start = None; sc_cool = 0.0; sc_dir_lbl = None; sc_dir = None
                fist_cf = 0; pinch_cf = 0; pinch_rf = 0
                rc_cf = 0; dc_cf = 0; ff_cf = 0; ff_rf = 0; neut_cf = 0
                alock = None
                pinch_rel = rc_rel = dc_rel = ff_rel = True

                if app_state == "DRAGGING":
                    fist_rf += 1
                    if fist_rf >= MIN_FIST_RELEASE:
                        try:
                            pyautogui.mouseUp(button="left")
                            hud_act = "DRAG END"; hud_exp = now + HUD_ACTION_DURATION
                        except Exception:
                            pass
                        app_state = "NORMAL"; fist_rf = 0
                elif app_state == "DRAG_ARMED":
                    app_state = "NORMAL"

            hist.append(raw)
            sg   = weighted_vote(hist)
            conf = gesture_conf(hist, sg)
            xtra = VELOCITY_EXTRA_CONFIRM if spd > VELOCITY_GATE_THR else 0
            ctrl = "IDLE"

            try:
                # ── NORMAL ────────────────────────────────────────────────────
                if app_state == "NORMAL":
                    if sg in ("POINT","OPEN PALM","FIST","NO HAND"):
                        neut_cf += 1
                        if (neut_cf >= MIN_NEUTRAL_CONFIRM and
                                alock in ("DOUBLE_CLICK","RIGHT_CLICK","PINCH","FOUR_FINGERS") and
                                now >= alock_t + ACTION_LOCK_MIN):
                            alock = None
                    else:
                        neut_cf = 0

                    if sg in ("POINT","OPEN PALM"):
                        pinch_cf = 0; pinch_rf += 1
                        if pinch_rf >= MIN_PINCH_RELEASE and now >= pinch_cool and alock is None:
                            pinch_rel = True
                    elif sg == "PINCH":
                        pinch_rf = 0
                    else:
                        pinch_cf = 0

                    if sg != "RIGHT CLICK":   rc_cf = 0
                    if sg not in ("RIGHT CLICK","DOUBLE CLICK","FOUR FINGERS","OPEN PALM"):
                        if now >= rc_cool and alock is None: rc_rel = True
                    if sg != "DOUBLE CLICK":  dc_cf = 0
                    if sg not in ("DOUBLE CLICK","FOUR FINGERS","OPEN PALM"):
                        if now >= dc_cool and alock is None: dc_rel = True

                    if sg in ("POINT","OPEN PALM","FIST","NO HAND"):
                        ff_cf = 0; ff_rf += 1
                        if ff_rf >= MIN_FF_RELEASE and now >= ff_cool and alock is None:
                            ff_rel = True
                    elif sg == "FOUR FINGERS":
                        ff_rf = 0
                    else:
                        ff_cf = 0

                    if sg != "POINT":
                        sc_start = None; sc_cool = 0.0; sc_dir_lbl = None
                        sc_dir = None; sc_dz = 0

                    # 1. POINT — move + scroll
                    if sg == "POINT" and itip is not None:
                        tx, ty = itip
                        rx = np.interp(tx, [MARGIN_X, fw - MARGIN_X], [0, screen_w])
                        ry = np.interp(ty, [MARGIN_Y, fh - MARGIN_Y], [0, screen_h])

                        # Velocity-adaptive C-D gain (Fitts\'s Law)
                        gain = cd.get(rx, ry)
                        if pmx is not None:
                            rx = pmx + (rx - pmx) * gain
                            ry = pmy + (ry - pmy) * gain

                        # 1€ Filter
                        sx = int(np.clip(fx(rx, ts), 2, screen_w - 3))
                        sy = int(np.clip(fy(ry, ts), 2, screen_h - 3))
                        pmx, pmy = float(sx), float(sy)
                        pyautogui.moveTo(sx, sy)
                        ctrl = "MOVING CURSOR"

                        cv2.circle(frame, (tx, ty), 11, (0, 240, 255), -1)
                        cv2.circle(frame, (tx, ty), 13, (0, 180, 200),  1, cv2.LINE_AA)

                        # Scroll
                        if over_app(sx, sy):
                            sc_start = None; sc_cool = 0.0; ctrl = "SCROLL BLOCKED"
                        else:
                            cy_tip = ty
                            if sc_start is None: sc_start = cy_tip
                            nm = time.monotonic()
                            dy = cy_tip - sc_start

                            if sc_dir == "UP" and dy > 0:
                                if now - sc_dir_t < REVERSAL_DELAY: sc_start = cy_tip; dy = 0
                            elif sc_dir == "DOWN" and dy < 0:
                                if now - sc_dir_t < REVERSAL_DELAY: sc_start = cy_tip; dy = 0

                            if abs(dy) <= SCROLL_DEADZONE:
                                sc_dz += 1
                                if sc_dz >= 6:
                                    sc_start = cy_tip
                                    if now - sc_dir_t >= REVERSAL_DELAY: sc_dir = None
                            else:
                                sc_dz = 0

                            if nm >= sc_cool:
                                ovsh = max(0.0, abs(dy) - SCROLL_TRIGGER_DIST)
                                clks = int(np.clip(SCROLL_BASE + ovsh * 2.5, SCROLL_BASE, SCROLL_MAX))

                                if dy <= -SCROLL_TRIGGER_DIST:
                                    if not (sc_dir == "DOWN" and now - sc_dir_t < REVERSAL_DELAY):
                                        try: pyautogui.scroll(clks)
                                        except Exception: pass
                                        hud_act = "SCROLL UP"; hud_exp = now + HUD_ACTION_DURATION
                                        sc_dir_lbl = "SCROLL UP"; sc_t = now
                                        sc_dir = "UP"; sc_dir_t = now
                                        sc_start = cy_tip; sc_cool = nm + SCROLL_COOLDOWN
                                    else:
                                        sc_start = cy_tip; sc_cool = nm + 0.12

                                elif dy >= SCROLL_TRIGGER_DIST:
                                    if not (sc_dir == "UP" and now - sc_dir_t < REVERSAL_DELAY):
                                        try: pyautogui.scroll(-clks)
                                        except Exception: pass
                                        hud_act = "SCROLL DOWN"; hud_exp = now + HUD_ACTION_DURATION
                                        sc_dir_lbl = "SCROLL DOWN"; sc_t = now
                                        sc_dir = "DOWN"; sc_dir_t = now
                                        sc_start = cy_tip; sc_cool = nm + SCROLL_COOLDOWN
                                    else:
                                        sc_start = cy_tip; sc_cool = nm + 0.12

                            if now - sc_t < 0.4 and sc_dir_lbl: ctrl = sc_dir_lbl

                    # 2. PINCH — click + arm drag
                    elif sg == "PINCH":
                        sc_start = None; sc_cool = 0.0
                        pinch_cf += 1
                        req = MIN_PINCH_CONFIRM + xtra
                        if alock is None and pinch_cf >= req and pinch_rel and now >= pinch_cool:
                            try:
                                p = pyautogui.position()
                                if over_app(p.x, p.y):
                                    ctrl = "CLICK BLOCKED"
                                else:
                                    pyautogui.click()
                                    hud_act = "LEFT CLICK"; hud_exp = now + HUD_ACTION_DURATION
                                    ctrl = "CLICK (PINCHED)"
                            except Exception:
                                ctrl = "CLICK ERROR"
                            alock = "PINCH"; alock_t = now
                            pinch_rel = False; pinch_rf = 0
                            pinch_cool = now + PINCH_COOLDOWN
                            app_state = "DRAG_ARMED"; drag_at = now; fist_cf = 0
                        else:
                            ctrl = "CLICK (PINCHED)"

                    # 3. RIGHT CLICK
                    elif sg == "RIGHT CLICK":
                        sc_start = None; sc_cool = 0.0; dc_cf = 0
                        rc_cf += 1
                        req = MIN_RC_CONFIRM + xtra
                        if alock is None and rc_cf >= req and rc_rel and now >= rc_cool:
                            try:
                                p = pyautogui.position()
                                if over_app(p.x, p.y): ctrl = "CLICK BLOCKED"
                                else:
                                    pyautogui.rightClick()
                                    hud_act = "RIGHT CLICK"; hud_exp = now + HUD_ACTION_DURATION
                                    ctrl = "RIGHT CLICK"
                            except Exception: ctrl = "RC ERROR"
                            alock = "RIGHT_CLICK"; alock_t = now
                            rc_rel = False; rc_cool = now + RC_COOLDOWN
                        else:
                            ctrl = "RIGHT CLICK"

                    # 4. DOUBLE CLICK
                    elif sg == "DOUBLE CLICK":
                        sc_start = None; sc_cool = 0.0; rc_cf = 0; pinch_cf = 0
                        if raw == "FOUR FINGERS" or "FOUR FINGERS" in hist: dc_cf = 0
                        else: dc_cf += 1
                        req = MIN_DC_CONFIRM + xtra
                        if alock is None and dc_cf >= req and dc_rel and now >= dc_cool:
                            try:
                                p = pyautogui.position()
                                if over_app(p.x, p.y): ctrl = "CLICK BLOCKED"
                                else:
                                    pyautogui.doubleClick()
                                    hud_act = "DOUBLE CLICK"; hud_exp = now + HUD_ACTION_DURATION
                                    ctrl = "DOUBLE CLICK"
                            except Exception: ctrl = "DC ERROR"
                            alock = "DOUBLE_CLICK"; alock_t = now
                            dc_rel = False; dc_cool = now + DC_COOLDOWN
                            rc_rel = False; rc_cool = now + 0.6
                        else:
                            ctrl = "DOUBLE CLICK"

                    # 5. FOUR FINGERS — browser back
                    elif sg == "FOUR FINGERS":
                        sc_start = None; sc_cool = 0.0
                        dc_cf = rc_cf = pinch_cf = 0
                        ff_cf += 1
                        req = MIN_FF_CONFIRM + xtra
                        if alock is None and ff_cf >= req and ff_rel and now >= ff_cool:
                            try:
                                p = pyautogui.position()
                                if over_app(p.x, p.y): ctrl = "NAV BLOCKED"
                                else:
                                    pyautogui.hotkey("alt","left")
                                    hud_act = "BROWSER BACK"; hud_exp = now + HUD_ACTION_DURATION
                                    ctrl = "BROWSER BACK"
                            except Exception: ctrl = "NAV ERROR"
                            alock = "FOUR_FINGERS"; alock_t = now
                            ff_rel = False; ff_rf = 0; ff_cool = now + FF_COOLDOWN
                            rc_rel = False; rc_cool = now + 0.8
                            dc_rel = False; dc_cool = now + 0.8
                        else:
                            ctrl = "BROWSER BACK"

                    # 6. OPEN PALM — pause
                    elif sg == "OPEN PALM":
                        pmx = pmy = None; ctrl = "CONTROL PAUSED"

                    # 7. FIST — idle
                    elif sg == "FIST":
                        pmx = pmy = None; ctrl = "IDLE (FIST)"

                # ── DRAG ARMED ────────────────────────────────────────────────
                elif app_state == "DRAG_ARMED":
                    sc_start = None; sc_cool = 0.0
                    if sg != "PINCH": pinch_cf = 0
                    if now - drag_at > DRAG_ARM_TIMEOUT:
                        app_state = "NORMAL"; fist_cf = 0; pinch_rf = 0; ctrl = "IDLE"
                    elif sg == "FIST":
                        fist_cf += 1
                        if fist_cf >= MIN_FIST_CONFIRM:
                            try:
                                pyautogui.mouseDown(button="left")
                                hud_act = "DRAG START"; hud_exp = now + HUD_ACTION_DURATION
                                app_state = "DRAGGING"; fist_rf = 0; fist_cf = 0
                                p = pyautogui.position(); pmx, pmy = float(p.x), float(p.y)
                                ctrl = "DRAGGING"
                            except Exception:
                                app_state = "NORMAL"; ctrl = "IDLE (FIST)"
                        else:
                            ctrl = "IDLE (FIST)"
                    elif sg in ("OPEN PALM","POINT","RIGHT CLICK","DOUBLE CLICK","FOUR FINGERS"):
                        app_state = "NORMAL"; fist_cf = 0; pinch_rf = 0; ff_rf = 0; alock = None
                        ctrl = {"OPEN PALM":"CONTROL PAUSED","POINT":"MOVING CURSOR",
                                "RIGHT CLICK":"RIGHT CLICK","DOUBLE CLICK":"DOUBLE CLICK",
                                "FOUR FINGERS":"BROWSER BACK"}.get(sg,"IDLE")
                    else:
                        ctrl = "CLICK (PINCHED)"

                # ── DRAGGING ──────────────────────────────────────────────────
                elif app_state == "DRAGGING":
                    sc_start = None; sc_cool = 0.0
                    if dtip is not None:
                        dx2, dy2 = dtip
                        rx2 = np.interp(dx2, [MARGIN_X, fw - MARGIN_X], [0, screen_w])
                        ry2 = np.interp(dy2, [MARGIN_Y, fh - MARGIN_Y], [0, screen_h])
                        gain2 = drag_cd.get(rx2, ry2)
                        if pmx is not None:
                            rx2 = pmx + (rx2 - pmx) * gain2
                            ry2 = pmy + (ry2 - pmy) * gain2
                        sx2 = int(np.clip(fx(rx2, ts), 2, screen_w - 3))
                        sy2 = int(np.clip(fy(ry2, ts), 2, screen_h - 3))
                        pmx, pmy = float(sx2), float(sy2)
                        pyautogui.moveTo(sx2, sy2)
                        cv2.circle(frame, (dx2, dy2), 13, (0, 165, 255), -1)
                        cv2.circle(frame, (dx2, dy2), 15, (0, 130, 200),  1, cv2.LINE_AA)
                    ctrl = "DRAGGING"
                    if sg == "FIST":
                        fist_rf = 0
                    else:
                        fist_rf += 1
                        if fist_rf >= MIN_FIST_RELEASE:
                            try: pyautogui.mouseUp(button="left")
                            except Exception: pass
                            hud_act = "DRAG END"; hud_exp = now + HUD_ACTION_DURATION
                            app_state = "NORMAL"; alock = None; fist_rf = 0
                            pinch_rel = (sg != "PINCH")

            except Exception as ex:
                print(f"Non-fatal: {ex}")

            # Status resolution
            trk = "ACTIVE" if res.multi_hand_landmarks else "NO HAND"
            if trk == "NO HAND":             cur_st = "IDLE"
            elif "BLOCKED" in ctrl:          cur_st = "BLOCKED"
            elif app_state == "DRAGGING":    cur_st = "DRAGGING"
            elif app_state == "DRAG_ARMED":  cur_st = "ARMED"
            elif sg == "OPEN PALM":          cur_st = "PAUSED"
            elif sg == "FIST":               cur_st = "IDLE"
            elif sg == "FOUR FINGERS":       cur_st = "BROWSER BACK"
            elif sg in ("POINT","PINCH","RIGHT CLICK","DOUBLE CLICK"): cur_st = "ACTIVE"
            else:                            cur_st = "IDLE"

            disp_act = hud_act if now <= hud_exp else "NONE"

            # Onboarding overlay
            if ob_active:
                rem = ONBOARDING_SECS - (now - ob_start)
                if rem > 0:
                    draw_onboarding(frame, rem)
                else:
                    ob_active = False

            # HUD
            draw_hud(frame, trk, sg, cur_st, disp_act,
                     fps, PRESETS[sidx]["name"], conf, spd, app_state)

            cv2.imshow(WINDOW_TITLE, frame)

            # Key handling
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                print("Quit."); break
            elif key == ord(" "):
                ob_active = False
            elif key in (ord("+"), ord("=")):
                sidx = min(sidx + 1, len(PRESETS) - 1)
                preset = PRESETS[sidx]
                fx.min_cutoff = fy.min_cutoff = preset["min_cutoff"]
                fx.beta = fy.beta = preset["beta"]
                cd.peak = drag_cd.peak = preset["cd_peak"]
                print(f"Sensitivity -> {preset['name']}")
            elif key in (ord("-"), ord("_")):
                sidx = max(sidx - 1, 0)
                preset = PRESETS[sidx]
                fx.min_cutoff = fy.min_cutoff = preset["min_cutoff"]
                fx.beta = fy.beta = preset["beta"]
                cd.peak = drag_cd.peak = preset["cd_peak"]
                print(f"Sensitivity -> {preset['name']}")

    except KeyboardInterrupt:
        print("Interrupted.")
    except Exception as err:
        import traceback
        print(f"Loop error: {err}"); traceback.print_exc()
    finally:
        if app_state == "DRAGGING":
            try: pyautogui.mouseUp(button="left")
            except Exception: pass
        hands.close(); cap.release(); cv2.destroyAllWindows()
        print("AeroClick stopped.")


if __name__ == "__main__":
    main()
