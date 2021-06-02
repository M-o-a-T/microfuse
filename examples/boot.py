# This file is executed on every boot (including wake-boot from deepsleep)
#import esp
#esp.osdebug(None)

import micropython
micropython.alloc_emergency_exception_buf(300)

import network
_wlan=network.WLAN(network.STA_IF)
_wlan.active(True)
_wlan.connect("you wish","well so do I")
_wlan.ifconfig(('10.1.2.3', '255.255.255.0', '10.1.2.1', '10.1.2.1'))

try:
	import webrepl
except ImportError:
	pass
else:
	webrepl.start()

import ufuse
U=ufuse.UFuse()
U.start()

import upin
P1=upin.UPin(U,13, topic="home/button/bell")
# that's enough to get the ball rolling
