#!/usr/bin/python3

import logging
import os
from concurrent.futures import CancelledError
from contextlib import asynccontextmanager, contextmanager

import anyio
from distmqtt.client import open_mqttclient

from .link import ServerError
from .util import ValueEvent, packer, stream_unpacker

logger = logging.getLogger(__name__)


class IsHandled:
    pass


async def handle_repl(client):
    async with client:
        name = await client.receive(1024)
        await client.send(b'Hello, %s\n' % name)


class ChatObj:
    """Local messaging"""

    def __init__(self, server):
        self.server = server
        self._next_id = 0
        self.ids = {}

    async def chat(self, complete_msg=False, **kw):
        """send a message"""
        self._next_id += 1
        cid = self._next_id
        evt = ValueEvent()
        self.ids[cid] = (evt, complete_msg)
        await self.server.submit(self, kw, cid)
        return await evt.get()

    def close(self):
        for e in self.ids.values():
            e[0].set_error(CancelledError())

    async def send(self, **kw):
        """called by the server with an incoming reply"""
        a = kw.get('a')
        evt, cm = self.ids.pop(kw.get('i'))
        if a == "r":
            evt.set(kw if cm else kw.get('d'))
        elif a == "e":
            evt.set_error(ServerError(kw.get('d')))
        else:
            raise ServerError(kw)


class REPL:
    """
    This class connects the multiplexer to a REPL client.

    The data stream consists of bytes from/to the MicroPython REPL.
    """

    _close = None
    _close_ind = False

    def __init__(self, mplex, sock):
        self.mplex = mplex
        self.sock = sock
        self.out_w, self.out_r = anyio.create_memory_object_stream(5)

    async def run(self, *, task_status=None):
        try:
            await self.mplex.send(a="ci", d=self.mplex.repl_nr)
            async with anyio.create_task_group() as tg:
                self._close = tg.cancel_scope
                tg.start_soon(self._reader)
                tg.start_soon(self._writer)
                if task_status:
                    task_status.started()
        finally:
            with anyio.move_on_after(2, shield=True):
                await self.sock.aclose()
            with anyio.move_on_after(2, shield=True):
                if not self._close_ind and not self.mplex.repl_multiplex:
                    await self.mplex.send(a="cx")

    async def _writer(self):
        """
        write repl data to the socket
        """
        while True:
            async for d in self.out_r:  # pylint: disable=E1133
                await self.sock.send(d)

    async def _reader(self):
        """
        read repl data from the socket
        """
        while True:
            d = await self.sock.receive()
            await self.mplex.send(a="c", d=d)

    async def send(self, data):
        await self.out_w.send(data)

    def close(self, ind=False):
        self._close_ind = ind
        self._close.cancel()


class Stream:
    """
    This class connects the multiplexer to a command client.

    The data stream consists of MsgPack messages.
    """

    _close = None

    def __init__(self, mplex, sock):
        self.mplex = mplex
        self.sock = sock
        self.packer = packer
        self.unpacker = stream_unpacker()
        self._send_lock = anyio.Lock()

    async def run(self):
        try:
            async with anyio.create_task_group() as tg:
                self._close = anyio.Event()
                tg.start_soon(self._reader)

                await self._close.wait()
                tg.cancel_scope.cancel()
        finally:
            with anyio.move_on_after(2, shield=True):
                await self.sock.aclose()

    async def _reader(self):
        while True:
            try:
                d = await self.sock.receive()
            except anyio.EndOfStream:
                return
            self.unpacker.feed(d)
            for msg in self.unpacker:
                await self._process(msg)

    async def _process(self, msg):
        if 'a' not in msg:
            logger.warning("Incoming unknown action: %r", msg)
            return
        seq = msg.pop('i', None)
        await self.mplex.submit(self, msg, seq)

    async def send(self, **kw):
        async with self._send_lock:
            await self.sock.send(self.packer(kw))

    async def aclose(self):
        self._close.set()


class Multiplexer:
    """
    This is the multiplexer object. It connects to the embedded system via
    a TCP socket. It offers a Unix socket for client programs like FUSE
    mounts, and another Unix socket to connect to the REPL.

    Unix socket paths are relative to XDG_RUNTIME_DIR if they don't contain a
    slash.
    """

    sock = None
    _cancel = None
    _tg = None
    mqtt = None

    def __init__(
        self,
        host,
        port,
        repl,
        stream,
        retry=0,
        mqtt=None,
        repl_nr=0,
        multiplex=False,
        watchdog=None,
    ):
        """
        Set up a MicroPython multiplexer.
        """
        self.host = host
        self.port = port
        try:
            rundir = os.environ["XDG_RUNTIME_DIR"]
        except KeyError:
            pass
        else:
            stream = os.path.join(rundir, stream)
            repl = os.path.join(rundir, repl)

        self.stream_path = stream
        self.repl_path = repl
        self.repl_nr = repl_nr
        self.repl_multiplex = multiplex
        self.repls = set()
        self.streams = dict()  # streamID > Stream
        self.mseq = dict()  # nr > streamID,seq
        self._send_lock = anyio.Lock()
        self.retry = retry
        self.h_evt = anyio.Event()
        self.mqtt_cfg = mqtt
        self.mqtt_sub = {}
        self.watchdog = watchdog

        self.packer = packer
        self.unpacker = stream_unpacker()

        self.next_mid = 0
        self.next_stream = 0
        self.next_sub = 0
        self.subs = {}  # nr > topic,codec,cs

    @asynccontextmanager
    async def _mqtt(self):
        if self.mqtt_cfg is None:
            yield self
            return
        async with open_mqttclient(config=dict(uri=self.mqtt_cfg)) as mqtt:
            try:
                self.mqtt = mqtt
                yield self
            finally:
                self.mqtt = None

    async def subscribed_mqtt(self, topic, raw=False, nr=None, *, task_status):
        """Forward this topic to the embedded system.

        This is a subtask.
        """
        if isinstance(topic, str):
            topic = topic.split("/")
        spl = '#' in topic or '+' in topic
        codec = None if raw else "msgpack"
        async with self.mqtt.subscription(topic, codec=codec) as sub:
            if nr is None:
                self.next_sub += 2
                nr = self.next_sub
                await self.send(a="ms", p=nr, d=topic)
            task_status.started(nr)
            try:
                with anyio.CancelScope() as cs:
                    self.subs[nr] = (topic, codec, cs)
                    async for msg in sub:
                        try:
                            if spl:
                                # wildcard resolution
                                w = []
                                rt = msg.topic.split("/")
                                for k in topic:
                                    if k == "+":
                                        w.append(rt[0])
                                    elif k == "#":
                                        w.extend(rt)
                                        break
                                    elif k != rt[0]:
                                        logger.warning(
                                            "Strange topic: %r vs %r", msg.topic, "/".join(topic)
                                        )
                                        continue
                                    rt = rt[1:]
                                await self.send(a="m", p=nr, d=msg.data, w=w)
                            else:
                                await self.send(a="m", p=nr, d=msg.data)
                        except Exception:
                            logger.exception("Received from %r", msg)

            except Exception:
                await self.send(a="mu", p=nr)
            finally:
                try:
                    del self.subs[nr]
                except KeyError:
                    pass

    async def run(self, *, task_status=None):
        async with self._mqtt(), anyio.create_task_group() as tg:
            self._tg = tg
            self._cancel = tg.cancel_scope

            retry = self.retry
            sleep = 0.1
            while True:
                try:
                    await tg.start(self._conn)
                except ConnectionRefusedError:
                    if not retry:
                        raise
                except OSError as e:
                    if e.__cause__ is None:
                        raise
                    if type(e.__cause__) is not ConnectionRefusedError:
                        raise
                    if not retry:
                        raise
                else:
                    break

                retry -= 1
                await anyio.sleep(sleep)
                sleep *= 1.5

            await tg.start(self._conn_init)
            if self.watchdog:
                await tg.start(self._watchdog)
                print(f"Watchdog: {self.watchdog} seconds")
            if self.repl_path:
                await tg.start(self._serve_repl, self.repl_path)
                print("REPL on %r", self.repl_path)
            if self.stream_path:
                await tg.start(self._serve_stream, self.stream_path)
                print("Commands on %r", self.stream_path)
            if task_status:
                task_status.started()
            print("Done.")

    async def aclose(self):
        for _, se in self.mseq.values():
            sid, seq = se
            rstream = self.streams[sid]
            msg = dict(a="e", d="closed", i=seq)

            try:
                with anyio.fail_after(2, shield=True):
                    await rstream.send(**msg)
            except Exception:
                logger.exception("Closing: Could not send %r to %r", msg, rstream)
                await rstream.aclose()
            except BaseException:
                await rstream.aclose()
                raise

        if self._cancel is not None:
            self._cancel.cancel()
            self._cancel = None

    async def _watchdog(self, *, task_status):
        await self.send(a="hw", d=self.watchdog * 2.2)
        task_status.started()
        with self.chat() as c:
            while True:
                await anyio.sleep(self.watchdog)
                await c.chat(a="p", w=True)

    async def _serve_repl(self, repl, *, task_status):
        listener = await anyio.create_unix_listener(repl)
        if task_status:
            task_status.started()
        await listener.serve(self._handle_repl)

    async def _serve_stream(self, stream, *, task_status=None):
        listener = await anyio.create_unix_listener(stream)
        if task_status:
            task_status.started()
        await listener.serve(self._handle_stream)

    async def _handle_repl(self, sock):
        repl = REPL(self, sock)
        self.repls.add(repl)
        try:
            await repl.run()
        except anyio.EndOfStream:
            pass
        except Exception:
            logger.exception("REPL Crash")
        finally:
            self.repls.remove(repl)

    @contextmanager
    def _attached(self, stream):
        self.next_stream += 1
        sid = self.next_stream

        stream._mplex_sid = sid
        self.streams[sid] = stream
        try:
            yield stream
        finally:
            del self.streams[sid]

    async def _handle_stream(self, sock):
        stream = Stream(self, sock)
        with self._attached(stream):
            try:
                await stream.run()
            except anyio.EndOfStream:
                pass
            except Exception as e:
                logger.exception("Stream Crash")
                try:
                    await stream.send(a='e', d=repr(e))
                except Exception:
                    pass

    @contextmanager
    def chat(self):
        s = ChatObj(self)
        try:
            with self._attached(s):
                yield s
        finally:
            s.close()

    async def send(self, **kw):
        """
        Send a message to the embedded device
        """
        async with self._send_lock:
            print("M SEND", kw)
            if self.sock is None:
                raise EOFError
            await self.sock.send(self.packer(kw))

    async def _conn_init(self, *, task_status):
        await self.send(a="h", d="multiplex")
        await self.h_evt.wait()
        task_status.started()

    async def _conn(self, *, task_status):
        try:
            async with await anyio.connect_tcp(self.host, self.port) as sock:
                self.sock = sock
                task_status.started()
                await self._reader()
        finally:
            self.sock = None

    async def _reader(self):
        while True:
            d = await self.sock.receive()
            self.unpacker.feed(d)
            for msg in self.unpacker:
                print("M RECV", msg)
                await self._process(msg)

    async def submit(self, serv, msg, seq):
        self.next_mid += 1
        mid = self.next_mid
        self.mseq[mid] = (serv._mplex_sid, seq)
        await self.send(i=mid, **msg)

    async def _to_repl(self, msg):
        for c in list(self.repls):
            try:
                with anyio.fail_after(0.1):
                    await c.send(msg)
            except Exception:
                logger.warning("Blocked REPL %r", c)
                c.close()

    async def _process(self, msg):
        a = msg.pop('a', '')

        try:
            cmd = getattr(self, "cmd_" + a)
        except AttributeError:
            logger.warning("Incoming unknown message: a=%r %r", a, msg)
            await self.send(a="e", i=msg.get('i'), d='?')
        else:
            try:
                d = await cmd(**msg)
            except Exception as e:
                logger.exception("Oops %r", msg)
                if a != 'e':
                    await self.send(a="e", i=msg.get('i'), d=repr(type(e)))
            else:
                i = msg.get('i')
                if (d is not None or i is not None) and d is not IsHandled:
                    await self.send(a="r", i=i, d=d)

    async def cmd_h(self, d=None, **_kw):
        print("Hello:", d)
        self.h_evt.set()

    async def cmd_p(self, d=None, **_kw):
        return d

    async def cmd_r(self, d=None, i=None, _cmd='r', **kw):
        if i is None:
            logger.info("Reply: %r %r", d, kw)
        else:
            try:
                sid, seq = self.mseq.pop(i)
                rstream = self.streams[sid]
                await rstream.send(a=_cmd, i=seq, d=d, **kw)
            except Exception:
                logger.exception("Could not handle reply: %r %r %r", i, d, kw)

        return IsHandled

    async def cmd_e(self, **kw):
        await self.cmd_r(_cmd='e', **kw)
        return IsHandled

    # REPL / Console

    async def cmd_c(self, d, **_kw):
        """console data"""
        for r in self.repls:
            await r.send(d)

    async def cmd_cx(self, **_kw):
        """console closed"""
        for r in self.repls:
            r.close(True)

    # Messaging

    async def cmd_m(self, p, d=None, w=(), r=False, **_kw):
        if isinstance(p, int):
            p, c, _ = self.subs[p]
        else:
            c = None if r else "msgpack"
        if isinstance(p, str):
            p = p.split("/")
        if w:
            pr = []
            for k in p:
                if k == "+":
                    k = w[0]
                    w = w[1:]
                elif k == "#":
                    pr.extend(w)
                    break
                pr.append(k)
            p = pr
        await self.mqtt.publish(p, d, codec=c)

    async def cmd_ms(self, d, p, r=False, **_kw):
        if isinstance(d, str):
            d = d.split("/")
        await self._tg.start(self.subscribed_mqtt, d, r, p)

    async def cmd_mu(self, p, **_kw):
        _, _, c = self.subs[p]
        c.cancel()
