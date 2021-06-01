#
# This example shows how to forward a de-bounced button to MQTT.
# The MQTT message contains the msgpack-encoded time (in msec) the button
# was pressed.
#
from machine import Pin as _Pin
from micropython import schedule as _sched
from time import ticks_ms, ticks_diff

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

    def _do_irq(self, _):
        if self._sched or self._dly is not None:
            return
        self._sched = True
        _sched(self._irq_, None)

    def _irq(self,_):
        self._sched = False
        if self.on is None:
            self.on = ticks_ms()

        if self._dly is not None:
            self.U.sched.cancel(self._dly)
        self._dly = self.U.sched.enter(self.delay,self._timer)

    def _timer(self):
        self._dly = None
        if not self.pin():
            self._dly = self.U.sched.enter(self.delay,self._timer)
            return

        t = ticks_diff(ticks_ms(),self.on)-self.delay
        if self.topic:
            self.U.publish(self.topic, t, exp=self.exp)
        print("PI",self.pin,t)

        self.on = None
