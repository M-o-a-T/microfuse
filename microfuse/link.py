import logging
from contextlib import asynccontextmanager

import anyio
from outcome import Error, Value

from microfuse.util import packer, stream_unpacker

from .util import CtxObj, NotGiven

logger = logging.getLogger(__name__)


async def handle_console(client):
    async with client:
        name = await client.receive(1024)
        await client.send(b'Hello, %s\n' % name)


class ServerError(RuntimeError):
    def __init__(self, s):
        super().__init__()
        self.s = s

    def __repr__(self):
        return "ServerError(%r)" % (self.s,)


class Link(CtxObj):
    sock = None
    seq = 0

    def __init__(self, link):
        self.link = link
        self.packer = packer
        self.unpacker = stream_unpacker()
        self._send_lock = anyio.Lock()
        self.waiting = dict()

    @asynccontextmanager
    async def _ctx(self):
        async with await anyio.connect_unix(self.link) as sock:
            self.sock = sock
            try:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(self._reader)

                    try:
                        yield self
                    finally:
                        tg.cancel_scope.cancel()
            finally:
                self.sock = None
                with anyio.move_on_after(2, shield=True):
                    await sock.aclose()

    async def _reader(self):
        while True:
            d = await self.sock.receive()
            self.unpacker.feed(d)
            for msg in self.unpacker:
                await self._process(msg)

    async def _process(self, msg):
        if 'a' not in msg:
            logger.warning("Incoming unknown action: %r", msg)
            return
        await getattr(self, "_cmd_" + msg['a'])(**msg)

    async def _cmd_r(self, i=None, _err=False, d=None, **_kw):
        if i:
            try:
                evt = self.waiting[i]
            except KeyError:  # may happen when closing
                pass
            else:
                self.waiting[i] = Error(ServerError(d)) if _err else Value(d)
                evt.set()
        else:
            await self.aclose()
            for i, evt in list(self.waiting.items()):
                self.waiting[i] = Error(ServerError(d)) if _err else Value(d)
                evt.set()
        return

    async def _cmd_e(self, **kw):
        await self._cmd_r(_err=True, **kw)

    async def send(self, a, d=NotGiven, **kw):
        async with self._send_lock:
            self.seq += 1
            kw['a'] = a
            kw['i'] = seq = self.seq
            if d is not NotGiven:
                kw['d'] = d
            self.waiting[seq] = evt = anyio.Event()
            try:
                await self.sock.send(self.packer(kw))
                await evt.wait()
                return self.waiting[seq].unwrap()
            finally:
                del self.waiting[seq]

    async def aclose(self):
        if self.sock is not None:
            await self.sock.aclose()
            self.sock = None

    # utility

    async def ping(self, msg=None, timeout=2):
        with anyio.fail_after(timeout):
            res = await self.send(a='p', d=msg)
        if msg != res:
            logger.error("Ping: %r vs %r", msg, res)

    @asynccontextmanager
    async def mount(self, path, blocksize=None, debug=False, *, task_status=None):
        import pyfuse3

        from .fuse import Operations

        operations = Operations(self)
        if blocksize:
            operations.max_read = blocksize
            operations.max_write = blocksize

        logger.debug('Mounting...')
        fuse_options = set(pyfuse3.default_options)  # pylint: disable=I1101
        fuse_options.add('fsname=microfs')
        fuse_options.add(f'max_read={operations.max_read}')
        if debug:
            fuse_options.add('debug')
        pyfuse3.init(operations, str(path), fuse_options)  # pylint: disable=I1101

        if task_status:
            task_status.started()

        logger.debug('Entering main loop..')
        async with anyio.create_task_group() as tg:
            try:
                tg.start_soon(pyfuse3.main)  # pylint: disable=I1101
                yield None

            finally:
                pyfuse3.close(  # pylint: disable=I1101  # was False but we don't continue
                    unmount=True
                )
                tg.cancel_scope.cancel()
