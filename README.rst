=========
MicroFUSE
=========

This is a file system plus console multiplexer for MicroPython.

On the embedded device, a small client runs in the background. It receives
file system commands, any other commands you might want to hook up, and
REPL input, and replies with the commands' results and/or REPL output.

The other side uses a multiplexer which exposes a messaging socket and a
bidirectional channel for raw REPL data. It can also connect to MQTT so
that messages may be relayed on a single connection.

A FUSE driver links the messaging socket to a convenient place in the
file system; it's also possible to send file system commands directly.

The REPL channel can be used with a standard Telnet client.
