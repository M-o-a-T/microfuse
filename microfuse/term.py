#!/usr/bin/env python
#
# Very simple serial terminal
#
# (c) 2021 Matthias Urlichs <matthias@urlichs.de>
# 
# Part of this file is copied from pySerial.
# https://github.com/pyserial/pyserial (C)2002-2020 Chris Liechti
# <cliechti@gmx.net>
#
# SPDX-License-Identifier:    BSD-3-Clause

from __future__ import absolute_import

import codecs
import os
import sys
import threading
import stat
import socket

from serial.tools.list_ports import comports
from serial.tools import hexlify_codec

# pylint: disable=wrong-import-order,wrong-import-position

codecs.register(lambda c: hexlify_codec.getregentry() if c == 'hexlify' else None)

from serial.tools.miniterm import key_description, Console, CRLF, CR, LF
from serial.tools.miniterm import NoTerminal, NoControls, Printable, Colorize, DebugIO
from serial.tools.miniterm import EOL_TRANSFORMATIONS, TRANSFORMATIONS

def comports(d):
    for f in os.listdir(d):
        if f[0] == ".":
            continue
        f = os.path.join(d,f)
        s = os.stat(f)
        if stat.S_ISSOCK(s.st_mode):
            yield f


def ask_for_port():
    """\
    Ask the user for a socket name.
    """
    d = os.environ["XDG_RUNTIME_DIR"]
    print('\n--- Available sockets in XDG_RUNTIME_DIR:\n', file=sys.stderr)
    ports = []
    for n, port in enumerate(sorted(comports(d)), 1):
        print('--- {:2}: {:20}'.format(n, port), file=sys.stderr)
        ports.append(port)
    while True:
        if ports:
            port = input('--- Enter socket index or path: ')
        else:
            port = input('--- Enter socket path: ')
        try:
            index = int(port) - 1
            if not 0 <= index < len(ports):
                print('--- Invalid index!', file=sys.stderr)
                continue
        except ValueError:
            pass
        else:
            port = ports[index]
        if not port:
            continue
        try:
            try:
                s = os.stat(port)
            except FileNotFoundError:
                p = os.path.join(os.environ["XDG_RUNTIME_DIR"], port)
                if p == port:  # happens when abslute path
                    raise
                s = os.stat(p)
                port = p
        except FileNotFoundError:
            print(f"I can't find {port !r}.", file=sys.stderr)
            continue
        if not stat.S_ISSOCK(s.st_mode):
            print(f"{port !r} is not a socket.", file=sys.stderr)
            continue
        return port


class Miniterm(object):
    """\
    Terminal application. Copy data from serial port to console and vice versa.
    Handle special keys from the console to show menu etc.
    """

    socket = None

    def __init__(self, socketname, echo=False, eol='crlf', filters=()):
        self.console = Console()
        self.socketname = socketname
        self.echo = echo
        self.raw = False
        self.input_encoding = 'UTF-8'
        self.output_encoding = 'UTF-8'
        self.eol = eol
        self.filters = filters
        self.update_transformations()
        self.exit_character = chr(0x1d)  # GS/CTRL+]
        self.menu_character = chr(0x14)  # Menu: CTRL+T
        self.alive = None
        self._reader_alive = None
        self.receiver_thread = None
        self.rx_decoder = None
        self.tx_decoder = None

    def _start_reader(self):
        """Start reader thread"""
        self._reader_alive = True
        # start serial->console thread
        self.receiver_thread = threading.Thread(target=self.reader, name='rx')
        self.receiver_thread.daemon = True
        self.receiver_thread.start()

    def _stop_reader(self):
        """Stop reader thread only, wait for clean exit of thread"""
        self._reader_alive = False
        self.receiver_thread.join()

    def start(self):
        """start worker threads"""
        self.alive = True
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socketname)
        self.socket = sock

        self._start_reader()
        # enter console->serial loop
        self.transmitter_thread = threading.Thread(target=self.writer, name='tx')
        self.transmitter_thread.daemon = True
        self.transmitter_thread.start()
        self.console.setup()

    def stop(self):
        """set flag to stop worker threads"""
        self.alive = False
        self.socket.close()

    def join(self, transmit_only=False):
        """wait for worker threads to terminate"""
        self.transmitter_thread.join()
        if not transmit_only:
            self.receiver_thread.join()

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def update_transformations(self):
        """take list of transformation classes and instantiate them for rx and tx"""
        transformations = [EOL_TRANSFORMATIONS[self.eol]] + [TRANSFORMATIONS[f]
                                                             for f in self.filters]
        self.tx_transformations = [t() for t in transformations]
        self.rx_transformations = list(reversed(self.tx_transformations))

    def set_rx_encoding(self, encoding, errors='replace'):
        """set encoding for received data"""
        self.input_encoding = encoding
        self.rx_decoder = codecs.getincrementaldecoder(encoding)(errors)

    def set_tx_encoding(self, encoding, errors='replace'):
        """set encoding for transmitted data"""
        self.output_encoding = encoding
        self.tx_encoder = codecs.getincrementalencoder(encoding)(errors)

    def dump_port_settings(self):
        """Write current settings to sys.stderr"""
        sys.stderr.write("\n--- Settings: {p.socketname}\n".format(p=self))
        sys.stderr.write('--- serial input encoding: {}\n'.format(self.input_encoding))
        sys.stderr.write('--- serial output encoding: {}\n'.format(self.output_encoding))
        sys.stderr.write('--- EOL: {}\n'.format(self.eol.upper()))
        sys.stderr.write('--- filters: {}\n'.format(' '.join(self.filters)))

    def reader(self):
        """loop and copy serial->console"""
        try:
            while self.alive and self._reader_alive:
                # read all that is there or wait for one byte
                data = self.socket.recv(1)
                if data:
                    if self.raw:
                        self.console.write_bytes(data)
                    else:
                        text = self.rx_decoder.decode(data)
                        for transformation in self.rx_transformations:
                            text = transformation.rx(text)
                        self.console.write(text)
        except Exception:
            self.alive = False
            self.console.cancel()
            raise       # XXX handle instead of re-raise?

    def writer(self):
        """\
        Loop and copy console->serial until self.exit_character character is
        found. When self.menu_character is found, interpret the next key
        locally.
        """
        menu_active = False
        try:
            while self.alive:
                try:
                    c = self.console.getkey()
                except KeyboardInterrupt:
                    c = '\x03'
                if not self.alive:
                    break
                if menu_active:
                    self.handle_menu_key(c)
                    menu_active = False
                elif c == self.menu_character:
                    menu_active = True      # next char will be for menu
                elif c == self.exit_character:
                    self.stop()             # exit app
                    break
                else:
                    #~ if self.raw:
                    text = c
                    for transformation in self.tx_transformations:
                        text = transformation.tx(text)
                    self.socket.sendall(self.tx_encoder.encode(text))
                    if self.echo:
                        echo_text = c
                        for transformation in self.tx_transformations:
                            echo_text = transformation.echo(echo_text)
                        self.console.write(echo_text)
        except:
            self.alive = False
            raise

    def handle_menu_key(self, c):
        """Implement a simple menu / settings"""
        if c == self.menu_character or c == self.exit_character:
            # Menu/exit character again -> send itself
            self.socket.sendall(self.tx_encoder.encode(c))
            if self.echo:
                self.console.write(c)
        elif c == '\x15':                       # CTRL+U -> upload file
            self.upload_file()
        elif c in '\x08hH?':                    # CTRL+H, h, H, ? -> Show help
            sys.stderr.write(self.get_help_text())
        elif c == '\x05':                       # CTRL+E -> toggle local echo
            self.echo = not self.echo
            sys.stderr.write('--- local echo {} ---\n'.format('active' if self.echo else 'inactive'))
        elif c == '\x06':                       # CTRL+F -> edit filters
            self.change_filter()
        elif c == '\x0c':                       # CTRL+L -> EOL mode
            modes = list(EOL_TRANSFORMATIONS)   # keys
            eol = modes.index(self.eol) + 1
            if eol >= len(modes):
                eol = 0
            self.eol = modes[eol]
            sys.stderr.write('--- EOL: {} ---\n'.format(self.eol.upper()))
            self.update_transformations()
        elif c == '\x01':                       # CTRL+A -> set encoding
            self.change_encoding()
        elif c == '\x09':                       # CTRL+I -> info
            self.dump_port_settings()
        #~ elif c == '\x01':                       # CTRL+A -> cycle escape mode
        #~ elif c == '\x0c':                       # CTRL+L -> cycle linefeed mode
        elif c in 'pP':                         # P -> change port
            self.change_port()
        elif c in 'zZ':                         # S -> suspend / open port temporarily
            self.suspend_port()
        elif c in 'bB':                         # B -> change baudrate
            self.change_baudrate()
        elif c in 'qQ':
            self.stop()                         # Q -> exit app
        else:
            sys.stderr.write('--- unknown menu character {} --\n'.format(key_description(c)))

    def upload_file(self):
        """Ask user for filename and send its contents"""
        sys.stderr.write('\n--- File to upload: ')
        sys.stderr.flush()
        with self.console:
            filename = sys.stdin.readline().rstrip('\r\n')
            if filename:
                try:
                    with open(filename, 'rb') as f:
                        sys.stderr.write('--- Sending file {} ---\n'.format(filename))
                        while True:
                            block = f.read(1024)
                            if not block:
                                break
                            self.socket.sendall(block)
                            # Wait for output buffer to drain.
                            sys.stderr.write('.')   # Progress indicator.
                    sys.stderr.write('\n--- File {} sent ---\n'.format(filename))
                except IOError as e:
                    sys.stderr.write('--- ERROR opening file {}: {} ---\n'.format(filename, e))

    def change_filter(self):
        """change the i/o transformations"""
        sys.stderr.write('\n--- Available Filters:\n')
        sys.stderr.write('\n'.join(
            '---   {:<10} = {.__doc__}'.format(k, v)
            for k, v in sorted(TRANSFORMATIONS.items())))
        sys.stderr.write('\n--- Enter new filter name(s) [{}]: '.format(' '.join(self.filters)))
        with self.console:
            new_filters = sys.stdin.readline().lower().split()
        if new_filters:
            for f in new_filters:
                if f not in TRANSFORMATIONS:
                    sys.stderr.write('--- unknown filter: {!r}\n'.format(f))
                    break
            else:
                self.filters = new_filters
                self.update_transformations()
        sys.stderr.write('--- filters: {}\n'.format(' '.join(self.filters)))

    def change_encoding(self):
        """change encoding on the serial port"""
        sys.stderr.write('\n--- Enter new encoding name [{}]: '.format(self.input_encoding))
        with self.console:
            new_encoding = sys.stdin.readline().strip()
        if new_encoding:
            try:
                codecs.lookup(new_encoding)
            except LookupError:
                sys.stderr.write('--- invalid encoding name: {}\n'.format(new_encoding))
            else:
                self.set_rx_encoding(new_encoding)
                self.set_tx_encoding(new_encoding)
        sys.stderr.write('--- serial input encoding: {}\n'.format(self.input_encoding))
        sys.stderr.write('--- serial output encoding: {}\n'.format(self.output_encoding))

    def change_port(self):
        """Have a conversation with the user to change the serial port"""
        with self.console:
            try:
                port = ask_for_port()
            except KeyboardInterrupt:
                port = None
        if port and port != self.socketname:
            # reader thread needs to be shut down
            self.stop()
            self.socketname = port
            self.start()
            sys.stderr.write('--- Port changed to: {} ---\n'.format(self.socketname))

    def get_help_text(self):
        """return the help text"""
        # help text, starts with blank line!
        return """
--- mpy-term --- help
---
--- {exit:8} Exit program (alias {menu} Q)
--- {menu:8} Menu escape key, followed by:
--- Menu keys:
---    {menu:7} Send the menu character itself to remote
---    {exit:7} Send the exit character itself to remote
---    {info:7} Show info
---    {upload:7} Upload file (prompt will be shown)
---    {repr:7} encoding
---    {filter:7} edit filters
--- Toggles:
---    {echo:7} echo  {eol:7} EOL
---
--- Port settings ({menu} followed by the following):
---    p          change port
""".format(exit=key_description(self.exit_character),
           menu=key_description(self.menu_character),
           rts=key_description('\x12'),
           dtr=key_description('\x04'),
           echo=key_description('\x05'),
           info=key_description('\x09'),
           upload=key_description('\x15'),
           repr=key_description('\x01'),
           filter=key_description('\x06'),
           eol=key_description('\x0c'))

