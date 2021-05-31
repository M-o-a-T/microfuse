=========
MicroFUSE
=========

This is a file system plus console plus MQTT plus take-your-pick
multiplexer for MicroPython.

On the embedded device, a small client runs in the background. It receives
file system commands, any other commands you might want to hook up, and
REPL input, and replies with the commands' results and/or REPL output.

The other side uses a multiplexer which exposes a messaging socket and a
bidirectional channel for raw REPL data. It can also connect to MQTT so
that messages may be relayed on a single connection.

A FUSE driver links the messaging socket to a convenient place in the
file system; it's also possible to send file system commands directly.

A terminal program (based on pyserial-miniterm) is included.

TODO
====

* The msgpack implementation needs improvement; it is crazy inefficient.
  Sending a console line *should* eat 50 bytes (one three-element dict to be
  transmitted) or zero bytes (if we hand-roll the message), not 1700.

* There is not yet any attempt at reconnecting. (Should there be?)

* There are occasional MicroPython crashes::

      assertion "ATB_GET_KIND(block) == AT_HEAD" failed: file "…/micropython/py/gc.c", line 591, function: gc_free

  This might be related to bytearray slicing.
