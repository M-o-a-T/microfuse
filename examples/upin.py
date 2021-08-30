#
# This example shows how to forward a de-bounced button to MQTT.
# The MQTT message contains the msgpack-encoded time (in msec) the button
# was pressed.
#
from time import ticks_diff, ticks_ms

from machine import Pin as _Pin
from micropython import schedule as _sched


class UPin:
    def __init__(self, U, pin_nr, delay=50, topic=None, exp=()):
        self.U = U
        self.pin = _Pin(pin_nr, _Pin.IN,_Pin.PULL_UP)

        self._irq_ = self._irq

        self.irq = self.pin.irq(self._do_irq)
        self.v = self.pin()
        self._dly = None
        self.delay = delay
        self.on = None
        self.topic = topic
        self.exp = exp
        self._sched = False
        self._scan_ = self.U.sched.enter(self.delay*5, self._scan)

    def _scan(self):
        if self._sched:
            self._irq()
        self.U.sched.enter(self.delay*5, self._scan_)

    def _do_irq(self, _):
        if self._sched or self._dly is not None:
            return
        self._sched = True
        try:
            _sched(self._irq_, None)
        except RuntimeError:
            self._sched = False # more luck next time?

    def _irq(self, _=None):
        self._sched = False
        if self.on is None:
            self.on = ticks_ms()

        if self._dly is not None:
            self.U.sched.cancel(self._dly)
        self._dly = self.U.sched.enter(self.delay,self._timer)

    def _timer(self):
        if not self.pin():
            self.U.sched.enter(self.delay,self._dly)
            return

        self._dly = None
        t = ticks_diff(ticks_ms(),self.on)-self.delay
        if self.topic:
            self.U.publish(self.topic, t, exp=self.exp)

        self.on = None
