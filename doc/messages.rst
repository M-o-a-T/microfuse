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


Actions
=======

c
-----

REPL/console I/O. Usually sent without an ID. Data is binary.

ci
-----

REPL Init. data: the terminal number (for "dupterm"), defaults to zero.

cx
-----
REPL exit. If sent to the multiplexer, disconnects the REPL client(s).

e
-----

Error reply. ``e`` contains the error message.

f?
-----

File system messages. See `file system`_.

h
-----

Hello, sent from both client and server after connection setup is completed.

ha
-----
Authorization. TODO.

hs
-----
STARTSSL. TODO.

hw
-----
Start the watchdog timer. Data is the timeout in seconds. If the message
has a 'p' key with a true-ish value, any incoming message will feed the
watchdog.

m
-----
MQTT Messaging. 'd' is the data, 'p' the topic number.

The message may be an arbitrary msgpack-able data structure.

If the topic (as transmitted in the ``ms`` message) contains a wildcard,
'w' contains a list with the substituted elements.

If send from the embedded system, the topic may also be a string or list.
In this case, if the message is a text or binary string it's transmitted
directly (UTF8-encoded, if non-binary).

This message doesn't trigger a reply.

ms
-----
Subscribe. 'd' is the topic to subscribe to, 'p' the topic number to use.

If 'r' is true-ish, binary string messages on this topic are transmitted
directly.

Both sides may send this message. The multiplexer shall generate even topic
numbers, odd numbers are controlled by the embedded system. After reconnecting,
both sides must resend their subscription requests. Duplicate subscriptions
for the same topic numbers are not an error.

mu
-----
Unsubscribe. 'p' is the topic number to release. The data

p
-----

Ping. If the message has a true-ish 'w' key, the watchdog will be fed.

r
-----

Standard reply. ``d`` contains the data, if any.


