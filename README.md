# chargegate

A charge-band guard for Linux phones that live plugged in.

Your battery ages fastest at 100%, sitting on a charger forever. chargegate
holds the cell in a configurable band (default 30–60%) by writing standard
Qualcomm `power_supply` sysfs attributes — no app, no vendor daemon, just a
single-file Python daemon speaking to the kernel's charger driver.

Built for a phone that runs 24/7 as a car-alarm brain on Ubuntu Touch. The
same mechanism works on other Linux-phone stacks whose kernels expose the
Qualcomm pmi8998-family charger attributes (verified on LineageOS via
`su`; see the Android notes below).

## Why

Lithium cells kept at high state-of-charge age fast — calendar wear at 100%
SoC is several times the wear at mid-band. A phone that lives on a charger
never cycles, so calendar aging is *all* it gets. Holding 30–60% roughly
halves that aging while keeping the phone always ready.

But naive `echo 0 > charging_enabled` scripts have a failure mode that
took months to map on a real device, and chargegate's reason to exist
is handling it:

- **The charge-path wedge.** Toggling `battery_charging_enabled` can leave
  the charger logic in a state where the cell silently takes nothing while
  reporting "Charging" — input powers the phone, the battery floats. The
  guard detects wedges via a frozen `charge_counter`, classifies them
  against cable faults by VBUS firmness, and **alerts**. A wedged phone is
  stable indefinitely (input carries the load, cell floats mid-band) —
  the danger is chronic under-charge, not drain.
- **Why the guard does NOT auto-pulse wedges.** The classic recovery is a
  `input_suspend` 1→0 pulse, and a manual pulse at the keyboard works.
  But two automated live attempts each ended with the kernel accepting the
  suspend write and rejecting every restore — leaving the input latched
  and draining the phone to death. On this hardware the kernel has two
  rejection modes (partial: a charge-path teardown recovers; total:
  nothing is accepted) and **they cannot be distinguished before
  writing**. An automated pulse converts a stable wedge into a coin-flip
  death. chargegate wedges are alert-only: a human decides, a human
  pulses, with a runbook.
- **Latched input.** If an input ever ends up suspended with a charger
  present, a safety watchdog fights it while alerting — and the runbook's
  teardown sequence (`charging_enabled=0` → pulse → restore) is the proven
  manual recovery. The guard can disable charging, but it can never leave
  the phone worse than it found it — that invariant is the whole design.

## Compatibility

Qualcomm-based phones with these `/sys/class/power_supply/battery`
attributes: `battery_charging_enabled`, `input_suspend`, `charge_counter`,
`capacity`, `current_now`, `status` (plus `usb/online` and `usb/voltage_now`
on the USB node). Check yours before installing:

```bash
python3 chargegate.py --probe
```

Exit 0 = core attributes present. The probe prints what your kernel
exposes and warns about anything missing.

Known-good: Poco X3 (Ubuntu Touch, kernel 4.14 pmi8998). Attributes also
present on OnePlus 5T (LineageOS) — same driver family.

## Install

chargegate needs root *only* for sysfs writes; it shells out to sudo per
write. Configure a password source and a Telegram bot for alerts.

- **Ubuntu Touch (recommended target):** run as a systemd user unit.
  1. `mkdir -p ~/.config/chargegate && cp chargegate.env ~/.config/chargegate/env`
  2. Edit the env file: `TELEGRAM_BOT_TOKEN`, `CHARGEGATE_CHAT_ID`,
     `CHARGEGATE_DEVICE_LABEL`, and `CHARGEGATE_SUDO_PASSWORD_CMD`
     (default `pass show chargegate/sudo` — any command that prints the
     sudo password works; NOPASSWD devices can set it to `true`).
  3. `cp chargegate.service ~/.config/systemd/user/ && systemctl --user enable --now chargegate`

- **Android (LineageOS etc.):** Android reaps background daemons, so run
  chargegate *on another Linux box* over SSH, writing to the phone via
  `su` (that is how the OnePlus 5T in our fleet is guarded), or in Termux
  with a wake-lock if you accept the reliability tradeoff. The sysfs
  interface is identical; only the privilege escalation differs.

## What it does

- Holds the band (default 30–60, tunable constants at the top of the file)
- Detects charge-path wedges by frozen `charge_counter` with taper-zone
  exclusion (never hunts near the band top where the counter legitimately
  freezes)
- Wedge response is **alert-only by design**: one alert + 6h reminders.
  The recovery pulse is deliberately not automated — see "Why the guard
  does NOT auto-pulse wedges" above. The repo ships the manual runbook
  instead
- Safety watchdog: any latched `input_suspend` with a charger present gets
  restore attempts with loud alerting (never silent retries)
- Critical-drain backstop: charger connected but cell draining below 15%
  alerts immediately
- Edge-triggered Telegram alerting: one message when a condition starts,
  reminders every 6h, one clear — never a message per poll

## Testing

```bash
python3 tests/test_guards.py
```

57 offline checks: wedge detection, pulse machinery, watchdog, alert
escalation, watchdog, gauge-dither immunity, alert-episode state machine.
No device needed.

## Hardware lessons encoded here

The file's comments are the real documentation — they record a year of
Qualcomm charger-driver behavior learned the hard way:

- `input_suspend=0` is rejected `Invalid argument` while the charge path is
  held; a latched input needs a full teardown (`charging_enabled=0` first)
  before the restore is accepted — in *partial*-rejection mode. In
  total-rejection mode nothing is accepted, and the only write the kernel
  may take is the one that latches the input. The modes cannot be told
  apart before writing; that is why the pulse is not automated
- The fuel gauge dithers a single LSB during wedges — resume detection
  requires sustained monotonic advance, not one lucky sample
- `charge_full_design` reads garbage (negative) on these kernels; use
  `charge_full`

## License

MIT — see LICENSE.