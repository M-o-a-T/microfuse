==============
Message format
==============

Messages are packaged with Msgpack. All messages are dicts, common keys are
1-char strings for saving memory.

Authorisation and encryption are TODO.

Pre-Defined keys:

- a
  Action. Messages without this key are generally invalid.

- i
  ID / sequence number. Messages with an ID get a same-ID reply.
  Messages without an ID are not replied to. An ID of ``None`` is treated
  as if it had not been sent.

- d
  Some generic action-specific data: console text, return value, error string, …


Action types
============

c
-----

Console characters. Usually sent without an ID. Data is binary.

e
-----

Error reply. ``e`` contains the error message.

h
-----

Hello, sent from both client and server after connection setup is completed.

hs
-----
STARTSSL. TODO.

ha
-----
Authorization. TODO.

p
-----

Ping.

r
-----

Standard reply. ``d`` contains the data, if any.


