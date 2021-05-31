=====
Usage
=====

Setup
=====

Embedded system
---------------

Upload ``ufuse`` and ``msgpack``, preferably by encoding them with ``mpy-cross``.

.. bash::

    mpy-cross embedded/ufuse.py -o /tmp/ufuse.mpy
    mpy-cross ../micropython-lib/msgpack/msgpack.py -o /tmp/msgpack.mpy
    # use the WebREPL or "mpy-repl mount" to upload

Then, on the MicroPython system:

.. python::

    import ufuse
    U=ufuse.Ufuse(port=18266)  # this is the default
    U.start()

This starts the MicroFuse client. It accepts a single connection from the
multiplexer. A new connection replaces the old one when it's authenticated
(after auth is implemented).

Multiplexer
-----------

Start ``mpy-link``. This command accepts these arguments:

* -h ‹address›

  Host to connect to. Mandatory.

* -p ‹portnr›

  Port to use. The default is 18266, as above.

* -d ‹path›

  Command socket. ``mpy-cmd`` connects to this socket. The default is
  ``$XDG_RUNTIME_DIR/mpy-cmd``. Use '-' to not offer a command socket
  (though I can't think of a reason not to).

* -r ‹path›

  REPL socket. ``mpy-repl`` connects to this. The default is
  ``$XDG_RUNTIME_DIR/mpy-repl``. Use '-' to not offer a REPL socket (you
  probably don't need it anyway).

* -R ‹number›

  REPL channel number. MicroPython supports duplicating your terminal to
  different channels. Typically WebREPL uses channel zero. On an ESP8266,
  the serial port uses channel 1.

  The default is zero. On an ESP32 this is the only available channel, as
  UART0 is handled with a differnt mechanism. You can use the `"UART" mod
  <https://github.com/smurfix/micropython/tree/uart>`_ to change this.

* -m

  REPL multiplexing. If set, you can connect multiple terminals and
  everything the MicroPython system sends is echoed to all of them.
  If not set, new connections are rejected with an error message.

* -b MQTT_URL

  Connect to a MQTT server (DistMQTT, Mosquitto, …).


Command handler
---------------

Run ``mpy-cmd -d ‹path› command ‹args›``. Possible commands are described
below.

REPL
----

Run ``mpy-repl -r ‹path›``.


