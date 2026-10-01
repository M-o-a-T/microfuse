#!/bin/sh
# Power off the ESP via GPIO 18 of the BCM2835 gpiochip, then cool down before
# a possible restart. Installed to /usr/lib/microfuse/gpio-off.sh and run as
# ExecStopPost by microfuse.service.
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

# Defensively ensure the line is driven before clearing it, in case
# ExecStartPre never completed.
echo "$pin" > /sys/class/gpio/export 2>/dev/null || true
echo out > "/sys/class/gpio/gpio${pin}/direction"
echo 0 > "/sys/class/gpio/gpio${pin}/value"

# Cooldown before the device is powered on again.
sleep 10
