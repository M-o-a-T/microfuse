import anyio
import asyncclick as click
from microfuse.util import packer,stream_unpacker
import logging
import errno
logger = logging.getLogger(__name__)

class IsHandled:
    pass

async def handle_console(client):
    async with client:
        name = await client.receive(1024)
        await client.send(b'Hello, %s\n' % name)

class Console:
    def __init__(self, mplex, sock):
        self.mplex = mplex
        self.sock = sock
        self.out_w,self.out_r = anyio.create_memory_object_stream(5)

    async def run(self,*,task_status=None):
        try:
            async with anyio.create_task_group() as tg:
                self._close = anyio.Event()
                tg.start_soon(self._reader)
                tg.start_soon(self._writer)
                if task_status: task_status.started()
                await self._close.wait()
                tg.cancel_scope.cancel()
        finally:
            with anyio.move_on_after(2, shield=True):
                await self.sock.aclose()

    async def _writer(self):
        """
        write console data to the socket
        """
        while True:
            async for d in self.out_r:
                await self.sock.write(d)


    async def _reader(self):
        """
        read console data from the socket
        """
        while True:
            d = await self.sock.receive()
            await self.mplex.send(a="c",d=d)

    async def send(self, data):
        await self.out_w.send(data)

    def close(self):
        self._close.set()


class Stream:
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
        await self.mplex.submit(self,msg,seq)


    async def send(self, **kw):
        async with self._send_lock:
            await self.sock.send(self.packer(kw))

    async def aclose(self):
        self._close.set()


class Multiplexer:
    sock = None
    _cancel = None

    def __init__(self, host,port, console, stream, retry=0):
        self.host = host
        self.port = port
        self.console_path = console
        self.stream_path = stream
        self.consoles = set()
        self.streams = dict() # streamID > Stream
        self.mseq = dict() # nr > streamID,seq
        self._send_lock = anyio.Lock()
        self.retry = retry
        self.h_evt = anyio.Event()

        self.packer = packer
        self.unpacker = stream_unpacker()

        self.next_mid = 0
        self.next_stream = 0

    async def run(self,*,task_status=None):
        async with anyio.create_task_group() as tg:
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
            if self.console_path:
                await tg.start(self._serve_console, self.console_path)
                print("Console on %r", self.console_path)
            if self.stream_path:
                await tg.start(self._serve_stream, self.stream_path)
                print("Commands on %r", self.stream_path)
            if task_status:
                task_status.started()
            print("Done.")

    async def aclose(self):
        for mid,se in self.mseq.values():
            sid,seq = se
            rstream = self.streams[sid]
            msg=dict(a="e",d="closed",i=seq)

            try:
                with anyio.fail_after(2,shield=True):
                    await rstream.send(**msg)
            except Exception as exc:
                logger.exception("Closing: Could not send %r to %r", msg,rstream)
                await rstream.aclose()
            except BaseException:
                await rstream.aclose()
                raise

        if self._cancel is not None:
            self._cancel.cancel()
            self._cancel = None

    async def _serve_console(self, console, *, task_status):
        listener = await anyio.create_unix_listener(console)
        if task_status:
            task_status.started()
        await listener.serve(self._handle_console)

    async def _serve_stream(self, stream, *, task_status=None):
        listener = await anyio.create_unix_listener(stream)
        if task_status:
            task_status.started()
        await listener.serve(self._handle_stream)

    async def _handle_console(self, sock):
        cons = Console(self,sock)
        self.consoles.add(cons)
        try:
            await cons.run()
        except Exception:
            logger.exception("Console Crash")
        finally:
            self.consoles.remove(cons)

    async def _handle_stream(self, sock):
        self.next_stream += 1
        sid = self.next_stream
        stream = Stream(self,sock)
        stream._mplex_sid = sid
        self.streams[sid] = stream
        try:
            await stream.run()
        except anyio.EndOfStream:
            pass
        except Exception as e:
            logger.exception("Stream Crash")
            try:
                await stream.send(a='e',d=repr(e))
            except Exception:
                pass
        finally:
            del self.streams[sid]

    async def send(self, **kw):
        """
        Send a message to the embedded device
        """
        async with self._send_lock:
            print("M SEND",kw)
            if self.sock is None:
                raise EOFError
            await self.sock.send(self.packer(kw))

    async def _conn_init(self, *, task_status):
        await self.send(a="h",d="multiplex")
        await self.h_evt.wait()
        task_status.started()

    async def _conn(self, *, task_status):
        try:
            async with await anyio.connect_tcp(self.host,self.port) as sock:
                self.sock = sock
                task_status.started()
                await self._reader()
        finally:
            self.sock = None

    async def _reader(self):
        while True:
            try:
                d = await self.sock.receive()
            except anyio.EndOfStream:
                return
            self.unpacker.feed(d)
            for msg in self.unpacker:
                print("M RECV",msg)
                await self._process(msg)

    async def submit(self, serv, msg, seq):
        self.next_mid += 1
        mid = self.next_mid
        self.mseq[mid] = (serv._mplex_sid,seq)
        await self.send(i=mid,**msg)

    async def _to_console(self, msg):
        for c in list(self.consoles):
            try:
                with anyio.fail_after(0.1):
                    await c.send(msg)
            except Exception:
                logger.warning("Blocked console %r", c)
                c.close()

    async def _process(self, msg):
        a = msg.pop('a','')

        try:
            cmd = getattr(self,"_cmd_"+a)
        except AttributeError:
            logger.warning("Incoming unknown message: a=%r %r", a,msg)
            await self.send(a="e",i=msg.get('i'),d='?')
        else:
            try:
                d = await cmd(**msg)
            except Exception as e:
                logger.exception("Oops %r",msg)
                if a != 'e':
                    await self.send(a="e",i=msg.get('i'),d=repr(type(e)))
            else:
                i = msg.get('i')
                if (d is not None or i is not None) and d is not IsHandled:
                    await self.send(a="r", i=i, d=d)

    async def _cmd_h(self, d=None, **kw):
        print("Hello:",d)
        self.h_evt.set()

    async def _cmd_c(self, d, **kw):
        for c in self.consoles:
            await self.consoles.send(d)

    async def _cmd_p(self, d, **kw):
        return repr(d)

    async def _cmd_r(self, d, i=None, _cmd='r', **kw):
        if i is None:
            logger.info("Reply: %r %r", d, kw)
        try:
            sid,seq = self.mseq.pop(i)
            rstream = self.streams[sid]
            await rstream.send(a=_cmd,i=seq,d=d,**kw)
        except Exception as exc:
            logger.exception("Could not handle reply: %r %r %r", i,d,kw)

        return IsHandled

    async def _cmd_e(self, **kw):
        await self._cmd_r(_cmd='e', **kw)
        return IsHandled


@click.command
@click.option("-h","--host", nargs=1, help="Address of the embedded system")
@click.option("-p","--port", type=int, default=8267, help="Port to use")
@click.option("-c","--console", help="Console socket")
@click.option("-s","--socket", help="Multiplex socket")
async def main(host,port,console,socket):
    mplex = Multiplex(host, port, console, stream)
    await mplex.run()
    

if __name__ == "__main__":
    main()
