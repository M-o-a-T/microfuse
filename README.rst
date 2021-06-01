=========
MicroFUSE
=========

This is a file system plus console plus MQTT plus take-your-pick
multiplexer for MicroPython.

On the embedded device, a small single-client server runs in the
background. It receives file system commands, REPL input, OTA updates,
MQTT messages, and/or any other commands you might want to hook up. It
sends MQTT messages, REPL output, and any replies your commands generate.

The other side (Linux, for now) starts a multiplexer which connects to that
mini-server and exposes a messaging socket and a bidirectional channel for
raw REPL data. It also connects to MQTT.

A FUSE driver links the messaging socket to a convenient place in the
file system; it's also possible to send file system commands directly.

A terminal program (based on pyserial-miniterm) is included.

TODO
====

* OTA updates.

* set RTC time

* support persistent storage in RTC

* File system commands from the command line and/or from an async Python program.

* The msgpack implementation needs improvement; it is crazy inefficient.
  Sending a console line *should* eat 50 bytes (one three-element dict to be
  transmitted) or zero bytes (if we hand-roll the message), not 1700.

* There is not yet any attempt at reconnecting. (Should there be?)

* There are occasional MicroPython crashes::

      assertion "ATB_GET_KIND(block) == AT_HEAD" failed: file "…/micropython/py/gc.c", line 591, function: gc_free

  This definitely looks like a MicroPython bug. The problem might be related to bytearray slicing.
