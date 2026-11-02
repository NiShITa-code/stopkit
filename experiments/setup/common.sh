# Steps shared by the setup scripts (sourced, not run). REPO must be set.

# This is a disposable test machine: stop Ubuntu's automatic package updates, so that they cannot hold the package lock
# during set-up or run during a trial.
prepare_machine() {
  systemctl disable --now unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer > /dev/null 2>&1 || true
  systemctl stop apt-daily.service apt-daily-upgrade.service > /dev/null 2>&1 || true
}

# The harnesses refuse a trial unless the kernel's clock tick is nominal (10000 us). On some cloud machines the clock
# needs a correction of about 100 ppm, which chrony applies by setting the tick to 10001 us. On this disposable machine,
# stop chrony and put the tick back, moving the correction into the clock's frequency so its rate stays right
# (experiments/setup/clock_tick.py). With chrony stopped the wall clock is also never stepped during a trial.
fix_clock() {
  systemctl disable --now chrony.service > /dev/null 2>&1 || true
  systemctl disable --now systemd-timesyncd.service > /dev/null 2>&1 || true
  python3 "$REPO/setup/clock_tick.py"
}
