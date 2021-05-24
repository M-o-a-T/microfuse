try:
    import network
except ImportError:
    network = None

from msgpack import Packer, Unpacker
from collections import deque
from time import sleep
import socket
import uselect
import uos
import errno
try:
    from machine import RTC
except ImportError:
    RTC = None
try:
    from uos import dupterm
except ImportError:
    def dupterm(x):
        return None

class IsHandled:
    pass

class UnknownCommand(Exception):
    def __init__(self, cmd):
        self.cmd = cmd
    def __repr__(self):
        return "UnknownCommand(%r)" % (self.cmd,)

class ServerError(Exception):
    def __init__(self, s):
        self.s = s
    def __repr__(self):
        return "ServerError(%r)" % (self.s,)

_S = None

def setup_conn(port, accept_handler):
    listen_s = socket.socket()
    listen_s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    ai = socket.getaddrinfo("0.0.0.0", port)
    addr = ai[0][4]

    listen_s.bind(addr)
    listen_s.listen(1)
    if accept_handler:
        try:
            listen_s.setsockopt(socket.SOL_SOCKET, 20, accept_handler)
        except TypeError:
            print("No auto accept.")
    if network is None:
        print("UFuse daemon started on port %d." % (port,))
    else:
        for i in (network.AP_IF, network.STA_IF):
            iface = network.WLAN(i)
            if iface.active():
                print("UFuse daemon started on %s:%d" % (iface.ifconfig()[0], port))
    return listen_s

class UFuseClient:
    def __init__(self, server, sock):
        self.server = server
        self.sock = sock
        self.sock.setblocking(False)
        self.buf = bytearray(128)

        self.packer = Packer(
            strict_types=False,
            use_bin_type=True,
        ).pack
        self.unpacker = Unpacker(
            strict_map_key=False,
            raw=False,
            use_list=False,
        )
        self.poll = uselect.poll()
        self.poll.register(sock,uselect.POLLIN)

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.server is not None:
            self.server.closed(self)
            self.server = None

    def start(self):
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, 20, self._read)
        except TypeError:
            print("No client auto read.")
        self.send(a="h")

    def _read(self):
        self.poll.poll()
        try:
            d = self.sock.readinto(self.buf)
            if not d:
                raise EOFError
        except Exception:
            self.close()
            raise
        else:
            self.unpacker.feed(self.buf[:d])
            for msg in self.unpacker:
                self._process(msg)

    def _process(self, msg):
        if 'a' not in msg:
            print("Not processed:",msg)
            return
        a = msg.get('a')
        i = msg.get('i',None)
        try:
            cmd = getattr(self.server,"_cmd_"+a)
        except AttributeError:
            raise UnknownCommand(a)

        try:
            r = cmd(self, msg)
            if (r is not None or i is not None) and r is not IsHandled:
                self.send(a='r',i=i,d=r)

        except ServerError as se:
            self.send(a='e',i=i,d=se.s)
        except Exception as e:
            self.send(a='e',i=i,d=repr(e))

    def send(self, _data=None, **kw):
        if not _data and not kw:
            raise RuntimeError("no data")
        if _data and kw:
            raise RuntimeError("data and kw")
        if kw:
            _data = kw
        self.sock.send(self.packer(_data))


class UFuse:
    listen_s = None
    using_console = False
    client = None
    clients = None  # type: set[UFuseClient]

    _fd_last = 0
    _fd_cache = None

    def __init__(self, port=8267):
        global _S
        if _S is not None:
            raise RuntimeError("Only one UFuse handler may run")
        _S = self

        self.port = port
        self.clients = set()

        if RTC is not None:
            self.RTC = RTC()

        # self.cons_buf = deque()
        self._fd_cache = dict()


    # generic calls

    def _cmd_c(self, client, msg):
        """Console"""
        self.to_console(msg["d"])

    def _cmd_e(self, client, msg):
        """Error Reply"""
        return IsHandled

    # cmd_f*: file system calls, see below

    def _cmd_h(self, client, msg):
        """Hello"""
        print("Hello received")
        if self.client is not None and self.client is not client:
            try:
                self.client.close()
            except Exception as e:
                client.send(a="c",d="Error closing old client: %r" % (e,))
        self.client = client
        self.clients.remove(client)

    def _cmd_p(self, client, msg):
        """Ping"""
        return msg['d']

    def _cmd_r(self, client, msg):
        """Reply"""
        # right now we don't send stuff thus there's no reply to handle
        raise NotImplementedError


    def _cmd_t(self, client, msg):
        """get/set time: tuple(Y M D  H M S  uS)"""
        # right now we don't send stuff thus there's no reply to handle
        d = msg["d"]
        if RTC is None:
            raise NotImplementedError
        if d:
            self.RTC.init(d)
        else:
            return self.RTC.now()


    # file system calls

    _fs_prefix = ""
    def _fsp(self, msg):
        p=msg["d"]
        if self._fs_prefix:
            p=self._fs_prefix+"/"+p
        if p == "":
            p = "/"
        return p

    def _fd(self, msg, drop=False):
        if drop:
            return self._fd_cache.pop(msg['d'])
        else:
            return self._fd_cache[msg['d']]

    def _add_f(self, f):
        self._fd_last += 1
        fd = self._fd_last
        self._fd_cache[fd] = f
        return fd

    def _del_f(self,fd):
        f = self._fd_cache.pop(fd)
        f.close()

    def _cmd_f_c(self, client, msg):
        """open, possibly chdir."""
        for v in self._fd_cache.values():
            v.close()
        self._fd_cache = dict()

        d = msg["d"]
        try:
            import os
            os.chdir(d)
        except AttributeError:
            if not d or d == "/":
                self._fs_prefix = ""
            elif d[0] == "/":
                self._fs_prefix = d
            else:
                self._fs_prefix += "/"+d

    def _cmd_fc(self, client, msg):
        """close"""
        f = self._fd(msg, drop=True)
        f.close()

    def _cmd_fd(self, client, msg):
        """dir"""
        p = self._fsp(msg)
        try:
            return uos.listdir(p)
        except AttributeError:
            return [ x[0] for x in uos.ilistdir(p) ]

    def _cmd_fg(self, client, msg):
        """getattr"""
        p = self._fsp(msg)
        try:
            s = uos.stat(p)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise ServerError("nf")
            raise
        if s[0] & 0x8000: # file
            return dict(m="f",s=s[6], t=s[7])
        elif s[0] & 0x4000: # file
            return dict(m="d", t=s[7])

    def _cmd_fn(self, client, msg):
        """new file"""
        p = self._fsp(msg)
        f = open(p,"wb")
        f.close()

    def _cmd_fo(self, client, msg):
        """open"""
        p = self._fsp(msg)
        try:
            f=open(p,msg['fm']+'b')
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise ServerError("nf")
            raise
        else:
            return self._add_f(f)

    def _cmd_fr(self, client, msg):
        """read"""
        f = self._fd(msg)
        f.seek(msg['fo'])
        return f.read(msg['fs'])

    def _cmd_fw(self, client, msg):
        """write"""
        f = self._fd(msg)
        f.seek(msg['fo'])
        return f.write(msg['fd'])

    def _cmd_fu(self, client, msg):
        """unlink"""
        p = self._fsp(msg)
        try:
            uos.remove(p)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise ServerError("nf")
            raise

    def _cmd_fU(self, client, msg):
        """unlink dir"""
        p = self._fsp(msg)
        try:
            uos.rmdir(p)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise ServerError("nf")
            raise

    def start(self):
        if self.listen_s is not None:
            raise RuntimeError("already running")
        self.listen_s = setup_conn(self.port, self._accept)

    def stop(self):
        if self.using_console:
            self.using_console = False
            dupterm(None)

        if self.listen_s is not None:
            self.listen_s.close()
            self.listen_s = None
        if self.client is not None:
            self.client.close()
            self.client = None
        while self.clients:
            client = self.clients.pop()
            client.close()

    def closed(self, client):
        if self.client is client:
            self.client = None
        else:
            self.clients.discard(client)

    def to_console(self, data):
        if self.using_console:
            self.cons_buf.append(data)
        else:
            print("CONS:",repr(data))

    def _accept(self, sock):
        client, remote_addr = sock.accept()
        ufc = UFuseClient(self, client)
        self.clients.add(ufc)
        ufc.start()


    # console methods
    def readinto(self, buf, nbytes=-1):
        if nbytes == -1:
            nbytes = len(buf)
        while not self.cons_buf:
            sleep(0.05)
        data = self.cons_buf.popleft()
        if len(data) <= nbytes:
            buf[:len(buf)] = data
            return len(data)
        else:
            buf[:nbytes] = data[0:nbytes]
            data = data[nbytes:]
            self.cons_buf.appendleft(data)
            return nbytes

    def read(self, nbytes=128):
        while not self.cons_buf:
            sleep(0.05)
        data = self.cons_buf.popleft()
        if len(data) <= nbytes:
            return data
        else:
            self.cons_buf.appendleft(data[nbytes:])
            return data[:nbytes]

    def readline(self):
        b = []
        while True:
            d = self.read()
            nl = d.find("\n")
            if nl >= 0:
                b.append(d[:nl])
                d = d[nl:]
                if d:
                    self.cons_buf.appendleft(d)
                return "".join(b)
            b.append(d)

    def write(self, buf):
        d = {"a":"c","b":buf}
        for c in list(self.clients):
            try:
                c.send(d)
            except Exception:
                c.close()

    def use_console(self):
        self.using_console = True
        old = dupterm(self)
        if old:
            old.close()

