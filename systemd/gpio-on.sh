#!/bin/sh
# Power on the ESP via GPIO 18 of the BCM2835 gpiochip, then let it settle.
# Installed to /usr/lib/microfuse/gpio-on.sh and run as ExecStartPre by
# microfuse.service.
set -eu

pin=""
for chip in /sys/class/gpio/gpiochip*; do
	[ -r "$chip/label" ] || continue
	if [ "$(cat "$chip/label")" = "pinctrl-bcm2835" ]; then
		base=$(cat "$chip/base")
		pin=$((base + 18))
		break
	fi
done

if [ -z "$pin" ]; then
	echo "microfuse: pinctrl-bcm2835 gpiochip not found" >&2
	exit 2
fi

# Export is idempotent: it errors (and is swallowed) if already exported.
echo "$pin" > /sys/class/gpio/export 2>/dev/null || true
echo out > "/sys/class/gpio/gpio${pin}/direction"
echo 1 > "/sys/class/gpio/gpio${pin}/value"

# Post-power-up settling time.
sleep 60
