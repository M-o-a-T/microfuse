# A generally useful event scheduler class.
#
# Scheduling runs in the background, courtesy of 'machine.schedule'.
#
# Events are specified by tuples (time, action, *argument, **kwargs).
# Unlike the standard sched class there's no priority, no runner
# function, and no locking.

import heapq
from collections import namedtuple
from time import ticks_diff, ticks_ms

from machine import Timer as _Timer
from micropython import schedule as _sched


def _cmp(s,o):
#   if s.time == o.time:
#       return s.priority - o.priority
    return ticks_diff(s.time, o.time)

class Event:
    def __init__(self, time, action, a, kw):
        self.time = time
        self.action = action
        self.a = a
        self.kw = kw
    def __eq__(s, o): return _cmp(s,o) == 0
    def __lt__(s, o): return _cmp(s,o) < 0
    def __le__(s, o): return _cmp(s,o) <= 0
    def __gt__(s, o): return _cmp(s,o) > 0
    def __ge__(s, o): return _cmp(s,o) >= 0

class Scheduler:
    _running = False
    def __init__(self, timer=-1):
        # setup. Pass in the timer# to use if not virtual.
        self._queue = []
        self._timer = _Timer(timer)
        self._run_ = self._run
        self._sched_run_ = self._sched_run

    def enter(self, _d_, _a_, *_a, **_kw):
        # usage: .enter(delay, proc, *args, **kw)
        # usage: .enter(delay, event)
        # delay in ms.
        t = ticks_ms()
        if type(_a_) is Event:
            _a_.time = t+_d_
        else:
            _a_ = Event(t + _d_, _a_, _a, _kw)
        heapq.heappush(self._queue, _a_)
        self._set_timer(t)
        return _a_

    def cancel(self, event):
        # Remove an event from the queue.
        self._queue.remove(event)
        heapq.heapify(self._queue)

    def _set_timer(self, t=None):
        if self._running:
            return
        if not self._queue:
            self._timer.deinit()
            return

        if t is None:
            t = ticks_ms()
        t = ticks_diff(self._queue[0].time, t)
        if t > 0:
            self._timer.init(mode=_Timer.ONE_SHOT, period=t, callback=self._sched_run_)
        else:
            _sched(self._run, None)

    def _sched_run(self, _):
        # runs in IRQ.
        try:
            _sched(self._run_, None)
        except RuntimeError:
            self._timer.init(mode=_Timer.ONE_SHOT, period=1, callback=self._sched_run_)

    def cancel(self, event):
        # Remove an event from the queue.
        self._queue.remove(event)
        heapq.heapify(self._queue)

    def empty(self):
        # Check whether the queue is empty."""
        return not self._queue

    def _run(self, _):
        # Execute events until the queue is empty.
        self._running = True
        try:
            q = self._queue
            pop = heapq.heappop
            while q:
                e = q[0]
                now = ticks_ms()
                if ticks_diff(e.time, now) > 0:
                    break
                else:
                    pop(q)
                    e.action(*e.a, **e.kw)
        finally:
            self._running = False
            self._set_timer()

    @property
    def queue(self):
        # An ordered list of upcoming events.
        events = self._queue[:]
        while events:
            yield heapq.heappop(events)

    def dump(self):
        print("now:",ticks_ms())
        for e in self.queue:
            print(e)
