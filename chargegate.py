#!/usr/bin/env python3
"""
Battery charge band guard for Linux phones with Qualcomm power_supply sysfs
(Ubuntu Touch, LineageOS, postmarketOS, ...).
Maintains battery charge between 40% and 60% to prolong battery life.
Detects charger connection and sends Telegram alerts for critical low battery.
Modified to use battery_charging_enabled instead of input_suspend.
Modified 2026-09-21: charge-path wedge detection (alert-only since the
  2026-09-22 incident - see handle_wedge).
Modified 2026-09-23: EDGE-TRIGGERED ALERTING (operator request: too much noise).
  Every condition messages once on entering, at most one reminder per
  6h while it persists, and one clear message where useful - instead of
  re-firing on a cooldown timer. The journal keeps the full cadence.
"""
import time
import os
import sys
import subprocess
import json

BATTERY_PATH = "/sys/class/power_supply/battery"
USB_PATH = "/sys/class/power_supply/usb"
CAPACITY_FILE = os.path.join(BATTERY_PATH, "capacity")
BATTERY_CHARGING_ENABLED_FILE = os.path.join(BATTERY_PATH, "battery_charging_enabled")
USB_ONLINE_FILE = os.path.join(USB_PATH, "online")
INPUT_SUSPEND_FILE = os.path.join(BATTERY_PATH, "input_suspend")
CHARGE_COUNTER_FILE = os.path.join(BATTERY_PATH, "charge_counter")
CURRENT_NOW_FILE = os.path.join(BATTERY_PATH, "current_now")
USB_INPUT_CURRENT_FILE = os.path.join(USB_PATH, "input_current_now")

LOW_THRESHOLD = 30
# Floor 40→30 (2026-10-11, operator: longevity is the whole point): halves the
# re-enable frequency — every 0→1 floor write is the wedge trigger (8/8
# incidents began within minutes of one). Lower average SoC also trims
# calendar aging, which dominates for a 24/7-plugged phone. Depth-of-cycle
# wear per full Ah rises at 30% depth vs 20%, but fewer cycles + lower
# average voltage net out roughly longevity-neutral, with the reliability
# win (half the wedge bets) as the real prize. Monitor thresholds
# (an external monitor's warn/crit levels should stay above the new floor's glide bottom)
HIGH_THRESHOLD = 60
CRITICAL_THRESHOLD = 15  # Alert when battery drops below this with no charger

# Charge-path wedge detection (2026-09-21 incident)
WEDGE_WINDOW_S = 600     # charge_counter must advance within this window

# Edge-triggered alerting (2026-09-23): a condition messages once when it
# starts, re-messages only every REMINDER interval while it persists, and
# once when it clears. Replaces cooldown-timer re-firing (a 6h wedge used
# to send 6 identical hourly messages).
WEDGE_REMINDER_S = 6 * 3600      # wedge: nudge every 6h until fixed
CRIT_REMINDER_S = 6 * 3600       # critical battery: nudge every 6h
INPUT_STUCK_REMINDER_S = 3600    # input latched suspended: hourly (phone is starving)
CC_RESUME_DELTA_UAH = 60000      # cc must advance this much before a resume is
                                 # even considered. Must exceed the pmi8998
                                 # gauge's ~50080 uAh LSB (2026-09-24: a single
                                 # up-LSB dither beat the old 25000 delta, faked
                                 # a clear, and the snapback re-armed a fresh
                                 # episode — 12 cycles, ~23 messages one night).
                                 # A crossing only ARMS a candidate; a further
                                 # monotonic step confirms (WedgeEpisode.update).

# Preconditioned auto-pulse (2026-10-07, operator: "Fix it for real" after the
# 9-day wedge). Rewrites the 2026-09-22 alert-only invariant with NARROW
# conditions — the exact state of all six clean manual pulses, which are
# also the states the 09-22 death structurally lacked:
#   flag=1, input_suspend=0 (charge path already un-held and un-latched),
#   charger present with FIRM VBUS (>=4.0V; amps lie during a wedge),
#   capacity below the taper zone (57%), cc frozen through a full 600s
#   detector window. The 09-22 misfire fired at the TAPER ZONE with flag
#   oscillations - both excluded here by construction.
# Safety rails:
#   - ONE attempt per wedge episode; a failed attempt leaves the episode
#     in the old alert-only mode (6h reminders) for a human.
#   - Hard verification within PULSE_VERIFY_TIMEOUT_S: input_suspend must
#     read 0 AND charge_counter must advance past CC_RESUME_DELTA_UAH.
#     A rejected restore (the 09-22 latched-suspend death) is CAUGHT here
#     and escalated loudly (input_stuck episode) instead of silently
#     starving the phone.
#   - A wedged phone is stable but chronically under-charged, never drains
#     to death on this path; a FAILED pulse can leave input latched - the
#     verification step exists to catch exactly that.
PULSE_ENABLED = True
PULSE_VERIFY_TIMEOUT_S = 90       # current_now flips within ~30s, cc steps an
                                 # LSB every few 30s loops -> 90s is ample
PULSE_MIN_VBUS_UV = 4000000      # below this, suspect cable/adapter, not PMIC
# Wedge maturity gate (2026-10-11, wedge #8 post-mortem): the kernel rejects
# ALL PMIC writes while a wedge is young — wedge #8 rejected input_suspend=0
# AND battery_charging_enabled=0 just 30 min in; wedge #7 accepted the full
# teardown at 3h; all six clean manual pulses were 2h+ old. Young wedges are
# reprogrammable only by reboot/UVLO — pulsing young turns a stable sitting
# wedge into the death spiral. So: let a fresh wedge SIT until it is mature
# (WEDGE_MATURITY_S after episode start), then pulse. A sitting wedge is
# stable (9-day precedent); a failed mature pulse falls back to the
# alert-only episode exactly as before.
WEDGE_MATURITY_S = 3 * 3600      # pulse only wedges at least 3h old

def get_sudo_password():
    """Get the sudo password by running CONFIG['sudo_password_cmd'].

    Default: `pass show chargegate/sudo` (password-store). Override with
    CHARGEGATE_SUDO_PASSWORD_CMD to use any password source that prints
    the password on stdout; devices with NOPASSWD sudo for the needed
    writes can point it at `true` (output ignored, rc 0).
    """
    try:
        result = subprocess.run(CONFIG['sudo_password_cmd'].split(),
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            print("Failed to get sudo password (rc={})"
                  .format(result.returncode))
            return None
    except Exception as e:
        print("Error running sudo password command: {}".format(type(e).__name__))
        return None

CONFIG = {
    "sudo_password_cmd": os.environ.get(
        "CHARGEGATE_SUDO_PASSWORD_CMD", "pass show chargegate/sudo"),
    "env_file": os.environ.get(
        "CHARGEGATE_ENV_FILE", os.path.expanduser("~/.config/chargegate/env")),
    "telegram_chat_id": os.environ.get("CHARGEGATE_CHAT_ID", ""),
    "device_label": os.environ.get("CHARGEGATE_DEVICE_LABEL", "device"),
}

def get_telegram_credentials():
    """Bot token from env var or the env file; chat id from CONFIG."""
    env_path = CONFIG["env_file"]
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = CONFIG["telegram_chat_id"]
    try:
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('TELEGRAM_BOT_TOKEN='):
                    token = line.split('=', 1)[1].strip().strip('"').strip("'")
                    break
    except Exception as e:
        print("Error reading .env: {}".format(e))
    return token, chat_id

def _telegram_send(message):
    """Send a Telegram message (no cooldown logic)."""
    token, chat_id = get_telegram_credentials()
    if not token:
        print("[Alert] No Telegram bot token found")
        return False

    try:
        import urllib.request
        import urllib.parse
        url = "https://api.telegram.org/bot{}/sendMessage".format(token)
        data = urllib.parse.urlencode({
            'chat_id': chat_id,
            'text': message,
        }).encode('utf-8')
        req = urllib.request.Request(url, data=data)
        resp = urllib.request.urlopen(req, timeout=10)
        if resp.status == 200:
            print("[Alert] Telegram alert sent")
            return True
        else:
            print("[Alert] Telegram returned status {}".format(resp.status))
            return False
    except Exception as e:
        print("[Alert] Error sending Telegram alert: {}".format(e))
        return False

class EpisodeAlerts:
    """Edge-triggered alert state (2026-09-23, operator: too much noise).

    A condition messages once when it STARTS, re-messages only every
    reminder_s while it persists, and once when it CLEARS. Replaces
    cooldown-timer re-firing, which re-sent identical messages on a
    schedule for as long as the condition sat there. Telegram is gated;
    the journal (print/log lines) is not.
    """

    def __init__(self):
        self.active = {}   # key -> {"started": ts, "last_msg": ts}

    def update(self, key, condition_now, now, reminder_s=6 * 3600):
        """Feed one sample. Returns 'start', 'reminder', 'clear', or None."""
        if condition_now:
            if key not in self.active:
                self.active[key] = {"started": now, "last_msg": now}
                return "start"
            if now - self.active[key]["last_msg"] >= reminder_s:
                self.active[key]["last_msg"] = now
                return "reminder"
            return None
        if key in self.active:
            del self.active[key]
            return "clear"
        return None

    def end_silent(self, key):
        """End an episode with no clear message (condition ended for
        benign/expected reasons — e.g. wedge hunting disarmed by taper)."""
        self.active.pop(key, None)


class WedgeEpisode:
    """Alert episode for the charge-path wedge.

    One full alert when the wedge is first detected, a short reminder every
    WEDGE_REMINDER_S while it persists, one clear message when
    charge_counter resumes. A wedge that outlives the charging-expected
    state (unplug, guard hold, taper zone) ends silently: not news, and a
    re-detection later starts a fresh episode.
    """

    def __init__(self, reminder_s=WEDGE_REMINDER_S):
        self.reminder_s = reminder_s
        self.alerts = EpisodeAlerts()
        self.live = False
        self.started_at = None    # epoch birth of the CURRENT episode (maturity gate)
        self.last_cc = None
        self.resume_cc = None   # first advance past the delta (candidate only)

    def update(self, detected, charging_expected, cc, now, info=None):
        """One guard-loop step. Returns (event, episode_dict_or_None).

        detected: WedgeDetector fired this loop.
        charging_expected: wedge hunting armed (flag=1, below taper zone).
        cc: current charge_counter reading.

        Resume confirmation (2026-09-24): during a wedge the pmi8998 gauge
        dithers a single ~50080 uAh LSB (observed 2103360<->2153440 all
        night). One up-LSB sample crossing the old delta faked a clear,
        and the snapback re-armed a fresh episode — 12 start/clear cycles
        (~23 messages) in one night, reminders never even reached. Now the
        first crossing past the delta only ARMS a resume candidate; a
        further monotonic step (cc > candidate) confirms and clears. Any
        regression cancels the candidate, and a fresh detection (cc sat
        frozen through a full window) kills it too. A real post-pulse
        charge steps one LSB every few 30s loops, so a genuine clear
        arrives a few minutes late; the dither never confirms.
        """
        ev = None
        if self.live:
            if not charging_expected:
                # unplug/hold/taper ended the hunt — silent, not news
                self.alerts.end_silent("wedge")
                self.live = False
                self.last_cc = None
                self.resume_cc = None
            elif (cc is not None and self.last_cc is not None
                    and cc > self.last_cc + CC_RESUME_DELTA_UAH):
                if self.resume_cc is None:
                    # first crossing: arm a candidate, do NOT clear yet
                    self.resume_cc = cc
                elif cc > self.resume_cc:
                    # sustained monotonic advance while charging expected:
                    # the charge path really resumed
                    self.live = False
                    self.last_cc = None
                    self.resume_cc = None
                    ev = self.alerts.update("wedge", False, now, self.reminder_s)
                # else cc == candidate: holding one gauge LSB — stay armed
            elif (self.resume_cc is not None and cc is not None
                    and cc < self.resume_cc):
                # regression below the candidate: the "advance" was gauge
                # dither, not a resume — cancel, episode continues
                self.resume_cc = None
        if detected:
            # fresh detection = cc sat frozen through a full window; any
            # pending resume candidate from dither is dead
            self.resume_cc = None
            if not self.live:
                # birth of a NEW episode: stamp it for the maturity gate
                # (young wedges reject all writes — wedge #8, 2026-10-11)
                self.started_at = now
            self.live = True
            if info and info.get("cc") is not None:
                self.last_cc = info["cc"]
            elif cc is not None and self.last_cc is None:
                self.last_cc = cc
            ev2 = self.alerts.update("wedge", True, now, self.reminder_s)
            if ev2:
                ev = ev2
        elif self.live and charging_expected:
            # no fresh detection, but episode persists: keep reminder timing
            # honest even if the detector's window wiggles between firings
            ev2 = self.alerts.update("wedge", True, now, self.reminder_s)
            if ev2:
                ev = ev2
        return ev, self.alerts.active.get("wedge")

def read_capacity():
    try:
        with open(CAPACITY_FILE, 'r') as f:
            return int(f.read().strip())
    except Exception as e:
        print("Error reading capacity: {}".format(e))
        return None

def read_charge_counter():
    try:
        with open(CHARGE_COUNTER_FILE, 'r') as f:
            return int(f.read().strip())
    except Exception:
        return None

def read_current_now():
    """Battery current in uA. Sign convention: negative = charging."""
    try:
        with open(CURRENT_NOW_FILE, 'r') as f:
            return int(f.read().strip())
    except Exception:
        return None

def _read_input_suspend():
    """Current input_suspend state ('0'/'1'), or None on read failure."""
    try:
        with open(INPUT_SUSPEND_FILE, 'r') as f:
            return f.read().strip()
    except Exception:
        return None

def read_usb_input_current_now():
    """Current the charger is actually delivering, in uA."""
    try:
        with open(USB_INPUT_CURRENT_FILE, 'r') as f:
            return int(f.read().strip())
    except Exception:
        return None

USB_VOLTAGE_FILE = os.path.join(USB_PATH, "voltage_now")

def read_usb_voltage_now():
    """VBUS in uV. Classification truth for wedge vs cable: 2026-09-23 a
    wedged PMIC UNDER-drew input (0.27-0.66A, below the old 'healthy'
    band) while VBUS stayed firm at 4.98V — amps mislead during a wedge,
    voltage does not."""
    try:
        with open(USB_VOLTAGE_FILE, 'r') as f:
            return int(f.read().strip())
    except Exception:
        return None

def charger_present(charger_connected, vbus_uv):
    """Charger-presence truth (2026-10-10): usb/online OR firm VBUS.

    A latched input_suspend kills usb/online (2026-10-08: it read 0 while
    VBUS was firm at 5.07V and the phone drained) — presence must fall
    back to VBUS. This is a PRESENCE predicate only: band management, the
    crit_plugged backstop, and wedge hunting all run under this gate.
    (Do NOT reuse watchdog_should_act here — that predicate also demands
    a latched input, and gating charge management on it disabled the
    whole band for 20h: a phone sailed 54%->100% with the guard 'healthy'.
    The 2026-10-10 incident.)"""
    return bool(charger_connected or (
        vbus_uv is not None and vbus_uv >= PULSE_MIN_VBUS_UV))


def watchdog_should_act(charger_connected, vbus_uv, charging_enabled, isusp):
    """Watchdog arming predicate (extracted 2026-10-09 for testability).

    True iff charging is expected (flag=1) AND input is latched AND a
    charger is REALLY present. Charger-present truth is VBUS, not
    usb/online: a latched input_suspend kills usb/online (2026-10-08:
    it read 0 while VBUS was firm at 5.07V and the phone drained) —
    gating on usb/online made the watchdog blind exactly when the input
    is latched, the one state it exists for."""
    return bool(charger_present(charger_connected, vbus_uv)
                and charging_enabled and (isusp == "1" or isusp is None))


def is_charger_connected():
    """Check if a charger is physically connected."""
    try:
        with open(USB_ONLINE_FILE, 'r') as f:
            return f.read().strip() == '1'
    except Exception:
        # Fallback: check battery status
        try:
            with open(os.path.join(BATTERY_PATH, "status"), 'r') as f:
                status = f.read().strip()
                return status in ("Charging", "Full")
        except Exception:
            return False

def is_charging_enabled():
    """Check if charging is enabled via battery_charging_enabled."""
    try:
        with open(BATTERY_CHARGING_ENABLED_FILE, 'r') as f:
            val = f.read().strip()
            return val == '1'
    except Exception:
        return False

def _sudo_sysfs_write(path, value, attempts=3):
    """Write a sysfs file via sudo tee, with retries.

    A write of 1 right after a 0 to battery_charging_enabled can transiently
    fail with 'Invalid argument' (seen 2026-09-21) - retry fixes it.
    """
    sudo_password = get_sudo_password()
    if not sudo_password:
        print("Cannot get sudo password")
        return False

    for attempt in range(attempts):
        try:
            proc = subprocess.Popen(['sudo', '-S', 'tee', path],
                                    stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    text=True)
            try:
                stdout, stderr = proc.communicate(
                    input=sudo_password + '\n' + str(value) + '\n', timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                print("sysfs write {}={} timed out (attempt {}/{})".format(
                    path, value, attempt + 1, attempts))
                continue
            if proc.returncode == 0:
                return True
            print("sysfs write {}={} failed (attempt {}/{}): {}".format(
                path, value, attempt + 1, attempts, (stderr or '').strip()))
        except Exception as e:
            print("Error writing {}={}: {}".format(path, value, e))
        time.sleep(3)
    return False

def set_charging_enabled(enable):
    """Set charging enabled or disabled via battery_charging_enabled."""
    value = '1' if enable else '0'
    if _sudo_sysfs_write(BATTERY_CHARGING_ENABLED_FILE, value):
        action = "enabled" if enable else "disabled"
        print("Charging {} successfully via battery_charging_enabled".format(action))
        return True
    else:
        print("Failed to set battery_charging_enabled to {}".format(value))
        return False

def pulse_input_suspend(escalate_on_reject=True):
    """Pulse input_suspend 1->0 to re-init the Qualcomm charge path.

    Callers: the preconditioned auto-pulse (2026-10-07) and manual runs.
    The restore write (input_suspend=0) can be REJECTED with `Invalid
    argument` while the charge path is held/wedged: on this PMIC a plain
    restore is kernel-rejected until the path is fully torn down first.
    2026-10-08 incident (wedge #7, first auto-pulse fire): naive retries
    of the identical rejected write latched the input and nearly killed
    the phone — the 09-22 anti-pattern in new code.

    ESCALATION SEQUENCE (proven live 2026-10-08, 3/3 remote recoveries
    incl. wedge #8 2026-10-09): if the plain restore is rejected, tear the
    charge path down completely and pulse again:
        charging_enabled=0 -> input_suspend=1 -> 3s -> input_suspend=0
        -> charging_enabled=1
    The kernel accepts the restore write from the torn-down state even
    when it rejects it from the held state.

    Verification remains the CALLER's duty (see attempt_auto_pulse): a
    True return means the writes were accepted, NOT that charging
    resumed. EVERY caller MUST verify input_suspend reads 0 and cc
    advances, and escalate loudly on failure.
    """
    if not _sudo_sysfs_write(INPUT_SUSPEND_FILE, "1"):
        print("WARNING: failed to suspend input; pulsing anyway")
    time.sleep(3)
    for attempt in range(2):
        if _sudo_sysfs_write(INPUT_SUSPEND_FILE, "0", attempts=1):
            return True
        print("WARNING: plain restore rejected ({}/2)".format(attempt + 1))
        time.sleep(3)
    if not escalate_on_reject:
        return False
    # ESCALATE: full charge-path teardown, then re-pulse. The pre-pulse
    # flag state was 1 (gate precondition) — remember it so we can restore
    # it; the teardown write itself forces input_suspend=1 (flag coupling).
    print("AUTO-PULSE ESCALATION: plain restore rejected - tearing charge path down")
    flag_was = is_charging_enabled()
    if flag_was:
        if not set_charging_enabled(False):
            print("WARNING: charging_enabled=0 write failed - continuing pulse anyway")
        time.sleep(2)
    if not _sudo_sysfs_write(INPUT_SUSPEND_FILE, "1"):
        print("WARNING: escalation suspend write failed")
    time.sleep(3)
    restored = False
    for attempt in range(3):
        if _sudo_sysfs_write(INPUT_SUSPEND_FILE, "0", attempts=1):
            restored = True
            break
        print("WARNING: escalated restore rejected ({}/3)".format(attempt + 1))
        time.sleep(3)
    if flag_was and not is_charging_enabled():
        if not set_charging_enabled(True):
            # loud: caller verification will catch a dead charge path;
            # leaving flag=0 here would glide the band into the floor
            print("WARNING: charging_enabled re-enable FAILED after escalation")
    return restored

def wedge_charging_expected(charging_now, cap):
    """True iff the wedge detector should be armed: charging enabled AND
    capacity below the taper zone (HIGH-3). At 57-60% current tapers
    naturally and charge_counter legitimately stops — hunting wedges there
    misreads taper as a wedge (2026-09-22 02:13 misfire, 59%)."""
    return bool(charging_now) and cap < HIGH_THRESHOLD - 3


# ---------------------------------------------------------------------------
# Preconditioned auto-pulse (2026-10-07). Episode-scoped: one attempt per
# wedge episode, tracked in module globals. A guard restart legitimately
# re-arms (a fresh boot is a new world: the PMIC state is unknown, the
# episode is not provably the same one).
# ---------------------------------------------------------------------------
_AUTO_PULSE = {
    "attempted": False,   # one attempt per wedge episode
    "in_flight": False,   # pulse/verify in progress (blocks band writes)
}

def auto_pulse_preconditions_met(episode_started_at=None):
    """ALL of the exact conditions of the six clean manual pulses (and the
    structural opposites of the 2026-09-22 death):
      - PULSE_ENABLED master switch
      - charger connected AND VBUS firm (>= 4.0V): input side healthy, this
        is a PMIC wedge, not a cable fault (2026-09-23 lesson: amps under-
        draw during a wedge, voltage does not)
      - charging flag = 1 (no coupling fight, no hold in effect)
      - input_suspend = 0 (charge path un-latched - the 09-22 killer was a
        REJECTED restore; we only ever pulse FROM a clean state, and the
        write of 1 always succeeds because the path is wedged, not held)
      - wedge is MATURE (>= WEDGE_MATURITY_S old): young wedges reject ALL
        PMIC writes (wedge #8, 2026-10-11: input_suspend=0 AND
        battery_charging_enabled=0 both rejected 30 min in; #7 accepted the
        full teardown at 3h; all six clean manual pulses were 2h+ old).
        A sitting wedge is stable (9-day precedent) - let it sit until it
        is old enough to accept the pulse. episode_started_at None = start
        time unknown (fresh module load, no episode object): treat as
        mature rather than never firing (a restart mid-episode must not
        strand the wedge until the next episode).
      - not attempted yet for this episode
    Returns (ok, reason_string) for journaling either way."""
    if not PULSE_ENABLED:
        return False, "auto-pulse disabled"
    # Charger presence: VBUS truth, not usb/online — during a wedge the
    # pmi8998 may legitimately report online=0 with firm VBUS (review
    # 2026-10-11, gpt-oss; 2026-10-08 latched-input precedent). Rejecting
    # the pulse for "no charger" on a wedged-but-powered phone would
    # disable the recovery exactly when it is needed.
    vbus_uv = read_usb_voltage_now()
    if not charger_present(is_charger_connected(), vbus_uv):
        return False, "no charger (usb/online=0 AND VBUS {}mV not firm)".format(
            (vbus_uv // 1000) if vbus_uv is not None else "unreadable")
    if vbus_uv is None or vbus_uv < PULSE_MIN_VBUS_UV:
        vbus_mv = (vbus_uv // 1000) if vbus_uv is not None else None
        return False, "VBUS {} mV not firm - cable/adapter fault, not PMIC".format(
            vbus_mv if vbus_mv is not None else "unreadable")
    if not is_charging_enabled():
        return False, "charging flag=0 (hold in effect or write failed)"
    isusp = _read_input_suspend()
    if isusp != "0":
        return False, "input_suspend={} (not 0 - charge path latched or unreadable)".format(isusp)
    if episode_started_at is not None:
        age_s = time.time() - episode_started_at
        if age_s < WEDGE_MATURITY_S:
            return False, "wedge age {:d}s < maturity {:d}s (young wedges reject all writes)".format(
                int(age_s), WEDGE_MATURITY_S)
    if _AUTO_PULSE["attempted"]:
        return False, "already attempted this episode"
    return True, "flag=1, input_suspend=0, VBUS {}mV, charger present, wedge mature".format(
        (vbus_uv // 1000) if vbus_uv else "?")

def _pulse_cc_baseline():
    return read_charge_counter()

def sync_auto_pulse_state(episode):
    """One pulse per wedge EPISODE, in both directions (2026-10-07).

    While an episode is live, a fired-and-failed attempt stays spent:
    reminders continue, no second pulse (the 09-22 lesson — repeated pulses
    on a rejecting PMIC are the death pattern). When the episode is OVER
    (cleared by resume-confirmation, or silently ended by unplug/hold/
    taper), re-arm the auto-pulse: the next wedge is a new episode and
    gets its own one attempt. in_flight must never be clobbered mid-pulse.
    None counts as no live episode (fresh start, pre-detection).
    """
    live = bool(episode is not None and episode.live)
    if not live and not _AUTO_PULSE["in_flight"]:
        _AUTO_PULSE["attempted"] = False

def attempt_auto_pulse(info, episode_started_at=None):
    """Fire ONE preconditioned pulse for this wedge episode, then verify.

    Verification (within PULSE_VERIFY_TIMEOUT_S):
      input_suspend reads 0 AND charge_counter advanced past
      CC_RESUME_DELTA_UAH from the pre-pulse baseline.
    On success: the episode clears via the normal WedgeEpisode resume path
      (cc advance arms + confirms the candidate) and the clear message goes
      out; no double-alert.
    On ANY failure: mark attempted, send ONE loud alert, then fall back to
      the old alert-only behavior (6h reminders) for a human.
    A failed restore (input latched) additionally arms input_stuck via the
      normal watchdog path next loop - never silently starving the phone.
    Young wedge (age < WEDGE_MATURITY_S): returns "deferred" WITHOUT
      spending the attempt - the call site retries on each loop until
      the episode is mature (wedge #8 lesson: young wedges reject all
      writes; a sitting wedge is stable).
    """
    ok, reason = auto_pulse_preconditions_met(episode_started_at)
    print("[{}] AUTO-PULSE gate: {} ({})".format(
        time.strftime('%H:%M:%S'), "PASS" if ok else "FAIL", reason))
    if not ok:
        if reason.startswith("wedge age"):
            return "deferred"
        return "skipped"
    _AUTO_PULSE["attempted"] = True
    _AUTO_PULSE["in_flight"] = True
    wrote = False
    verified = False
    cc0 = None
    try:
        cap = read_capacity()
        cc0 = _pulse_cc_baseline()
        vbus_uv = read_usb_voltage_now()
        print("[{}] AUTO-PULSE firing: cap {}%, cc {}, VBUS {}mV".format(
            time.strftime('%H:%M:%S'), cap, cc0,
            (vbus_uv // 1000) if vbus_uv else "?"))
        wrote = pulse_input_suspend()
        if wrote:
            deadline = time.time() + PULSE_VERIFY_TIMEOUT_S
            while time.time() < deadline:
                time.sleep(5)
                if _read_input_suspend() == "1":
                    print("[{}] AUTO-PULSE verify: input_suspend read back 1 - restore REJECTED, aborting".format(
                        time.strftime('%H:%M:%S')))
                    break
                if _read_input_suspend() == "0" and cc0 is not None:
                    cc = read_charge_counter()
                    if cc is not None and cc >= cc0 + CC_RESUME_DELTA_UAH:
                        verified = True
                        print("[{}] AUTO-PULSE verify: input restored, cc {} -> {} (+{} uAh)".format(
                            time.strftime('%H:%M:%S'), cc0, cc, cc - cc0))
                        break
    except Exception as e:
        # in_flight MUST clear even on exceptions (deepseek review 2026-10-07:
        # a stuck in_flight blocks sync re-arm forever, silently disabling
        # the auto-pulse). Re-raise nothing - treat as failed pulse.
        print("[{}] AUTO-PULSE exception: {} - treating as failed pulse".format(
            time.strftime('%H:%M:%S'), e))
    finally:
        _AUTO_PULSE["in_flight"] = False
    if verified:
        print("[{}] AUTO-PULSE: wedge cleared automatically".format(time.strftime('%H:%M:%S')))
        # No direct message: the caller sends the compact AUTO-CLEARED text;
        # the WedgeEpisode resume path confirms via cc advance.
        return "verified"
    # Failed: one loud alert, then human reminder cadence takes over.
    isusp = _read_input_suspend()
    _telegram_send(
        "{} charge-path wedge AUTO-PULSE FAILED (attempt 1/1 for this episode).\n"
        "Wrote input_suspend pulse {} — could not verify charge resume within {}s.\n"
        "input_suspend now reads {}. If 1: input latched - RE-PLUG USB or REBOOT the phone.\n"
        "Falling back to manual-run reminders (every {}h). cc {} -> {}.".format(
            CONFIG["device_label"], "OK" if wrote else "FAILED",
            PULSE_VERIFY_TIMEOUT_S, isusp,
            WEDGE_REMINDER_S // 3600, cc0, read_charge_counter()))
    return "failed"


class WedgeDetector:
    """Detects a wedged charge path.

    Charging is expected (charger connected, battery_charging_enabled=1,
    capacity below the high ceiling) but charge_counter does not advance.
    current_now is collected for diagnostics only; charge_counter is the
    truth (current_now is noisy near zero).
    """

    def __init__(self, window_s=WEDGE_WINDOW_S):
        self.window_s = window_s
        self.start_time = None
        self.start_cc = None
        self.currents = []

    def reset(self):
        self.start_time = None
        self.start_cc = None
        self.currents = []

    def update(self, charging_expected, cc, current_ua, now=None):
        """Feed one sample; returns a dict when a wedge is detected."""
        if not charging_expected or cc is None:
            self.reset()
            return None
        now = time.time() if now is None else now
        if current_ua is not None:
            self.currents.append(current_ua)
        if self.start_cc is None:
            self.start_time = now
            self.start_cc = cc
            return None
        if now - self.start_time < self.window_s:
            return None
        frozen = cc <= self.start_cc
        info = None
        if frozen:
            avg = None
            if self.currents:
                avg = int(sum(self.currents) / len(self.currents))
            info = {
                "window_s": int(now - self.start_time),
                "cc": cc,
                "avg_current_ua": avg,
            }
        # restart the window regardless of outcome
        self.start_time = now
        self.start_cc = cc
        self.currents = []
        return info

EPISODES = EpisodeAlerts()


def episode_alert(key, condition_now, reminder_s, msg_fn, clear_fn=None):
    """Edge-triggered Telegram alert for a boolean condition (2026-09-23).

    msg_fn(event) builds the start/reminder text; clear_fn() the clear text
    (both called only when a message is actually due). On a FAILED send the
    episode is dropped so the next loop retries — cooldown stamps only on
    success (2026-09-22 review rule). Returns the event or None."""
    ev = EPISODES.update(key, condition_now, time.time(), reminder_s)
    if ev in ("start", "reminder"):
        if _telegram_send(msg_fn(ev)):
            return ev
        EPISODES.active.pop(key, None)
        print("[Alert] send failed for '%s' — will retry next loop" % key)
    elif ev == "clear" and clear_fn:
        _telegram_send(clear_fn())
    return ev


def handle_wedge_event(event, ep, info):
    """Send the right message for a wedge-episode transition
    (2026-09-23 edge-triggered model): one full runbook alert on start,
    a short reminder every WEDGE_REMINDER_S while frozen, one clear when
    charge_counter resumes. Alert-only since the 2026-09-22 incident —
    never writes PMIC state."""
    cap = read_capacity()
    cc = read_charge_counter()
    input_now = read_usb_input_current_now()
    vbus_uv = read_usb_voltage_now()
    vbus_mv = (vbus_uv // 1000) if vbus_uv else None
    ts = time.strftime('%H:%M:%S')

    if event == "start":
        print("[{}] Charge-path WEDGE detected: capacity {}%, cc {} frozen {}s, "
              "avg current {}uA, input {}uA, VBUS {}mV".format(
                  ts, cap, cc, (info or {}).get('window_s'),
                  (info or {}).get('avg_current_ua'), input_now, vbus_mv))
        if vbus_mv and vbus_mv >= 4000:
            cause = ("PMIC wedge: VBUS firm at {}mV — a wedged PMIC "
                     "UNDER-draws input, so input amps are not truth here "
                     "(2026-09-23). The guard now attempts ONE preconditioned "
                     "input_suspend pulse per episode (2026-10-07); receiving "
                     "this start message means the attempt was skipped or "
                     "failed. Manual pulse: input_suspend 1->3s->0 with "
                     "charging flag=1, then watch usb_online flip to 1 "
                     "and current_now go negative.").format(vbus_mv)
        else:
            cause = ("Input side starved (VBUS {}mV) — likely cable/adapter; "
                     "swap the cable before suspecting the phone. (Auto-pulse "
                     "is skipped on soft VBUS - never pulses a starved input.)"
                     ).format(vbus_mv if vbus_mv is not None else "unreadable")
        _telegram_send(
            "{} CHARGE-PATH WEDGE\n"
            "Charge frozen at {}% (flag=1, charge_counter {}, input {} uA, "
            "VBUS {} mV).\n"
            "{}\n"
            "One reminder every {}h only while this persists."
            "".format(CONFIG["device_label"], cap, cc, input_now, vbus_mv, cause,
                       WEDGE_REMINDER_S // 3600))
    elif event == "reminder":
        print("[{}] Charge-path WEDGE still present: capacity {}%, cc {}".format(ts, cap, cc))
        _telegram_send(
            "{} charge-path wedge STILL present at {}% (cc {}) — {}h reminder. "
            "Auto-pulse was attempted and did not clear it (or was skipped); "
            "manual input_suspend pulse needed at the device.".format(
                CONFIG["device_label"], cap, cc, WEDGE_REMINDER_S // 3600))
    elif event == "clear":
        print("[{}] Charge-path wedge CLEARED: cc {} advancing, capacity {}%".format(ts, cc, cap))
        _telegram_send(
            "{} charge-path wedge CLEARED: charge_counter resumed ({}), "
            "capacity {}%.".format(CONFIG["device_label"], cc, cap))

def touch_heartbeat():
    """Mark the guard as alive for an external monitor (2026-09-23 convention).

    Written every loop pass; an external monitor reads its mtime ON the device (clock-skew
    safe). Fresh heartbeat -> an external monitor's backstop stays silent and
    lets this guard alert (ends the 09-21/09-23 double-fires). Missing file
    (reboot wiped tmpfs) or stale mtime -> guard dead -> backstop active."""
    try:
        with open("/tmp/chargegate_heartbeat", "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass  # heartbeat is best-effort; never block the guard loop


def main():
    try:
        import sys
        sys.stdout.reconfigure(line_buffering=True)  # truthful journal timestamps
    except Exception:
        pass
    print("Battery guard started - maintaining charge between {}% and {}%".format(LOW_THRESHOLD, HIGH_THRESHOLD))
    print("Critical alert threshold: {}%".format(CRITICAL_THRESHOLD))
    print("Charger detection: enabled")
    print("Telegram alerts: enabled")
    print("Accelerometer: NOT FOUND on this system")
    print("Using battery_charging_enabled for charging control")
    print("Charge-path wedge detection: enabled ({}s window, ALERT-ONLY + input-suspend watchdog)".format(WEDGE_WINDOW_S))

    check_interval = 30  # Check every 30 seconds
    last_status_print = 0
    wedge = WedgeDetector()
    wedge_ep = WedgeEpisode()

    try:
        while True:
            cap = read_capacity()
            if cap is None:
                time.sleep(5)
                continue

            charger_connected = is_charger_connected()

            # SAFETY WATCHDOG (2026-09-22, revised after external review): a
            # latched input_suspend starves the phone — but ONLY when charging
            # is expected. With flag=0 (intentional band hold) input_suspend=1
            # is the coupled, EXPECTED state on this PMIC; restoring it would
            # release the hold, the band logic would re-hold, and the loop
            # would flap 0<->1 every 30s — the rapid-toggle pattern that wedges
            # the charge path (2026-09-21). Watchdog acts iff flag=1 AND
            # input latched AND charger present.
            _isusp = _read_input_suspend()
            _vbus = read_usb_voltage_now()
            if watchdog_should_act(charger_connected, _vbus,
                                   is_charging_enabled(), _isusp):
                print("[{}] SAFETY: input_suspend=1 with charging expected - restoring".format(time.strftime('%H:%M:%S')))
                # Reuse the proven escalation pulse: from a latched state
                # the plain restore is kernel-rejected until the charge
                # path is torn down (2026-10-08). pulse_input_suspend
                # tries the plain restore first, then escalates — its
                # flag teardown/re-restore is exactly the 09-22 runbook.
                restored = pulse_input_suspend(escalate_on_reject=True)
                if restored:
                    time.sleep(2)
                    restored = _read_input_suspend() == "0"
                if not restored:
                    # flag is already 1 (precondition) — if the kernel still
                    # rejects the unsuspend, this is the 2026-09-22 incident
                    # state. Alert; never auto-escalate further.
                    pass
                if restored:
                    print("[{}] SAFETY: input restored".format(time.strftime('%H:%M:%S')))
                    # deepseek review 2026-10-09: an escalation that restored
                    # input but left charging_enabled=0 is a half-finished
                    # teardown — the phone would silently glide to the floor
                    # with the charger connected. Treat as NOT restored.
                    if not is_charging_enabled():
                        print("[{}] SAFETY: input restored but charging flag "
                              "still 0 - treating as failure".format(time.strftime('%H:%M:%S')))
                        restored = False
                else:
                    print("[{}] SAFETY: input WILL NOT restore - alerting".format(time.strftime('%H:%M:%S')))
                    episode_alert(
                        "input_stuck", True, INPUT_STUCK_REMINDER_S,
                        lambda ev: "{} BATTERY INPUT STUCK SUSPENDED with charger connected!\n"
                                   "Guard cannot restore input_suspend=0; phone will drain and die.\n"
                                   "Manual fix: re-plug USB cable or reboot phone. (safety watchdog, "
                                   "%s reminder)".format(CONFIG["device_label"]) % (
                                   "hourly" if INPUT_STUCK_REMINDER_S <= 3600
                                   else "%dh" % (INPUT_STUCK_REMINDER_S // 3600)))
            else:
                # watchdog restored the input (or it was never latched) —
                # close any open input_stuck episode
                episode_alert(
                    "input_stuck", False, INPUT_STUCK_REMINDER_S, None,
                    lambda: "{} input_suspend recovered (safety watchdog).".format(CONFIG["device_label"]))

            if charger_present(charger_connected, _vbus):
                # CRITICAL-DRAIN BACKSTOP (external+model review 2026-09-22): a
                # charger that is connected but not delivering (wedged PMIC,
                # failed charging write, latched input the watchdog could not
                # restore) drains the phone with NO other alert on this path.
                # Gate on VBUS-aware presence since 2026-10-09: a latched
                # input kills usb/online — the old gate went silent during
                # exactly the drain the backstop exists for (deepseek review).
                # 2026-10-10 incident: this gate was briefly watchdog_should_act
                # (hardcoded isusp="0" -> always False) — it disabled band
                # management for 20h. Never gate charge management on the
                # watchdog predicate; presence is a separate concern.
                episode_alert(
                    "crit_plugged", cap <= CRITICAL_THRESHOLD, CRIT_REMINDER_S,
                    lambda ev: "{} BATTERY CRITICAL: %d%% while plugged in but not "
                               "charging! Charge path wedged or write failed - check "
                               "the device. (critical-drain backstop)".format(
                               CONFIG["device_label"]) % cap,
                    lambda: "{} battery out of critical-while-plugged (%d%%).".format(
                               CONFIG["device_label"]) % cap)

                # Normal charge management when charger is connected
                charging = is_charging_enabled()

                if cap >= HIGH_THRESHOLD:
                    desired_charging = False
                elif cap <= LOW_THRESHOLD:
                    desired_charging = True
                else:
                    desired_charging = charging  # Keep current state in band

                if desired_charging != charging:
                    if set_charging_enabled(desired_charging):
                        state_str = 'ENABLED' if desired_charging else 'DISABLED'
                        print("[{}] Battery at {}% - charging {} (charger connected)".format(
                            time.strftime('%H:%M:%S'), cap, state_str))
                    else:
                        print("[{}] Failed to set charging state".format(time.strftime('%H:%M:%S')))

                # Wedge detection: charging expected -> charge_counter must advance.
                # NEVER hunt near the band top: current tapers naturally
                # approaching the hold ceiling and cc legitimately stops (the
                # 2026-09-22 02:13 misfire happened at 59% exactly this way).
                # Real toggle-wedges pin mid-band.
                charging_now = is_charging_enabled()
                charging_expected = wedge_charging_expected(charging_now, cap)
                w = wedge.update(charging_expected,
                                read_charge_counter(),
                                read_current_now())
                ev, ep = wedge_ep.update(
                    w is not None, charging_expected, read_charge_counter(),
                    time.time(), info=w)
                if ev == "start" and w is not None:
                    # Fresh wedge detection: try the preconditioned auto-pulse
                    # BEFORE building any alert (2026-10-07). On a verified
                    # clear the episode ends here with ONE compact message;
                    # on a fired-but-failed pulse the loud failure alert from
                    # attempt_auto_pulse IS the start message (reminders then
                    # follow the normal 6h cadence); on a gate skip (cable
                    # fault, latched input, hold) the original start alert
                    # with its cause classification still applies.
                    outcome = attempt_auto_pulse(w, wedge_ep.started_at)
                    if outcome == "verified":
                        cap_now = read_capacity()
                        _telegram_send(
                            "{} charge-path wedge AUTO-CLEARED ({}%). "
                            "Preconditioned input_suspend pulse fired, verified: "
                            "charge_counter resumed ({} -> {}). No action needed."
                            "".format(cap_now, w.get('cc'),
                                      read_charge_counter()))
                        # close the just-armed episode silently; the clear
                        # story is this message, not a follow-up
                        wedge_ep.alerts.end_silent("wedge")
                        wedge_ep.live = False
                        wedge_ep.started_at = None
                        wedge_ep.last_cc = None
                        wedge_ep.resume_cc = None
                        sync_auto_pulse_state(wedge_ep)   # re-arm for next episode
                    elif outcome == "failed":
                        # attempt_auto_pulse already sent the failure alert;
                        # episode stays armed for 6h reminders. No start text.
                        # (Reminder seeding is NOT needed: EpisodeAlerts
                        # tracks cadence off the start event the episode
                        # already emitted on detection; a failed pulse does
                        # not un-live the episode, so reminders fire on the
                        # normal 6h cadence from the episode clock. Verified
                        # by wedge_reminder_after_failed_pulse test.)
                        pass
                    elif outcome == "deferred":
                        # Too young to pulse (wedge #8 lesson). Send the
                        # normal start alert so a human knows NOW, then let
                        # the maturity re-check below fire the pulse when
                        # the episode is WEDGE_MATURITY_S old.
                        handle_wedge_event(ev, ep, w)
                    else:
                        handle_wedge_event(ev, ep, w)
                elif ev:
                    handle_wedge_event(ev, ep, w)
                # Maturity re-check (2026-10-11): a live, unattempted,
                # gate-clean episode that was DEFERRED as too young gets its
                # pulse the moment it matures - not at the next detector
                # firing (which may be hours away) and never twice.
                if (wedge_ep.live
                        and wedge_ep.started_at is not None
                        and not _AUTO_PULSE["attempted"]
                        and not _AUTO_PULSE["in_flight"]
                        and time.time() - wedge_ep.started_at >= WEDGE_MATURITY_S
                        and w is not None):
                    cap_now = read_capacity()
                    print("[{}] maturity re-check: episode {}s old, pulse due".format(
                        time.strftime('%H:%M:%S'),
                        int(time.time() - wedge_ep.started_at)))
                    outcome = attempt_auto_pulse(w, wedge_ep.started_at)
                    if outcome == "verified":
                        _telegram_send(
                            "{} charge-path wedge AUTO-CLEARED ({}%) by MATURE "
                            "pulse (deferred at detection, fired {:d}h in). "
                            "charge_counter resumed. No action needed.".format(
                                cap_now,
                                int((time.time() - wedge_ep.started_at) // 3600)))
                        wedge_ep.alerts.end_silent("wedge")
                        wedge_ep.live = False
                        wedge_ep.started_at = None
                        wedge_ep.last_cc = None
                        wedge_ep.resume_cc = None
                    sync_auto_pulse_state(wedge_ep)
                # episode-over re-arm: covers every non-start exit path
                # (resume-confirmed clear, unplug/hold/taper silent ends)
                sync_auto_pulse_state(wedge_ep)
            else:
                # No charger connected - ensure charging is enabled for when it's plugged in
                wedge.reset()
                # Unplug ends any wedge episode (charging cannot be expected
                # without input). sync_auto_pulse_state re-arms the auto-pulse
                # so the next wedge - possibly on a fresh charger session -
                # gets its own one attempt (review 2026-10-07: without
                # this, an episode ended by unplug left 'attempted' spent
                # and blocked every future pulse until restart).
                wedge_ep.alerts.end_silent("wedge")
                wedge_ep.live = False
                wedge_ep.started_at = None
                wedge_ep.last_cc = None
                wedge_ep.resume_cc = None
                sync_auto_pulse_state(wedge_ep)
                if is_charging_enabled() == False:
                    set_charging_enabled(True)

                # Critical low battery, no charger (edge-triggered episode):
                # start when it enters critical unplugged, one reminder per
                # 6h, clear when a charger returns or capacity recovers.
                episode_alert(
                    "crit_nocharger", cap <= CRITICAL_THRESHOLD, CRIT_REMINDER_S,
                    lambda ev: "{} BATTERY CRITICAL: {}% with NO charger connected! "
                               "Battery guard cannot maintain charge without a power "
                               "source. Plug in the charger at the device.".format(
                               CONFIG["device_label"], cap),
                    lambda: "{} critical-unplugged episode over ({}%, {}).".format(
                        CONFIG["device_label"], cap,
                        "charger connected" if charger_connected else "recovered"))
                if cap <= CRITICAL_THRESHOLD:
                    print("[{}] CRITICAL: Battery at {}%, NO CHARGER!".format(
                        time.strftime('%H:%M:%S'), cap))
            # charger present: close any open unplugged-crit episode
            if charger_connected:
                episode_alert("crit_nocharger", False, CRIT_REMINDER_S, None)

            # Periodic status print (every 5 min)
            now = time.time()
            touch_heartbeat()
            if now - last_status_print >= 300:
                status = "plugged in" if charger_connected else "NO CHARGER"
                cc = read_charge_counter()
                cur = read_current_now()
                print("[{}] Battery: {}% ({}) cc={} i={}uA".format(
                    time.strftime('%H:%M:%S'), cap, status,
                    cc if cc is not None else "n/a",
                    cur if cur is not None else "n/a"))
                last_status_print = now

            time.sleep(check_interval)

    except KeyboardInterrupt:
        print("\nBattery guard stopped.")
    except Exception as e:
        print("Error in battery guard: {}".format(e))
        import traceback
        traceback.print_exc()

def probe():
    """Report which power_supply attributes this kernel exposes.

    Exit code 0 = chargegate is likely compatible (core attributes present);
    1 = missing core attributes — charging control will not work here.
    """
    import platform
    print("chargegate compatibility probe")
    print("kernel: {}".format(platform.release()))
    bat = os.path.join(BATTERY_PATH)
    usb = os.path.join(USB_PATH)
    core = ["battery_charging_enabled", "capacity", "charge_counter",
            "input_suspend", "current_now", "status"]
    useful = ["charging_enabled", "voltage_now", "charge_full"]
    usb_core = ["online", "voltage_now"]
    missing_core = []
    for attr in core:
        p = os.path.join(bat, attr)
        if os.path.exists(p):
            try:
                val = open(p).read().strip()
            except Exception:
                val = "<unreadable>"
            print("  battery/{} = {}".format(attr, val))
        else:
            missing_core.append(attr)
            print("  battery/{} — MISSING".format(attr))
    for attr in useful:
        p = os.path.join(bat, attr)
        if os.path.exists(p):
            try:
                val = open(p).read().strip()
            except Exception:
                val = "<unreadable>"
            print("  battery/{} = {} (optional)".format(attr, val))
        else:
            print("  battery/{} — optional, missing".format(attr))
    for attr in usb_core:
        p = os.path.join(usb, attr)
        if os.path.exists(p):
            try:
                val = open(p).read().strip()
            except Exception:
                val = "<unreadable>"
            print("  usb/{} = {}".format(attr, val))
        else:
            print("  usb/{} — MISSING (wedge classification degrades)".format(attr))
    ok = not missing_core
    if ok:
        print("CORE ATTRIBUTES PRESENT — chargegate should work here.")
        print("Caveats: this guard encodes pmi8998-family charge-path quirks")
        print("(input_suspend wedge, teardown-restore escalation). Verify the")
        print("band behavior with a low-stakes device before trusting it.")
    else:
        print("MISSING CORE: {} — charging control will NOT work; do not run "
              "the guard here.".format(", ".join(missing_core)))
    return ok


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        sys.exit(0 if probe() else 1)
    main()