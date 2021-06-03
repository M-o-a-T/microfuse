try:
    import network
except ImportError:
    network = None

import errno
import socket
from time import sleep

import uos
import uselect
from msgpack import Packer, Unpacker
from uio import IOBase

try:
    from micropython import schedule
except ImportError:
    schedule = lambda x,y:x(y)
try:
    from machine import WDT
except ImportError:
    WDT = None

try:
    _dtn = uos.dupterm_notify
except AttributeError:
    _dtn = None

try:
    from machine import RTC
except ImportError:
    RTC = None
try:
    from uos import dupterm
except ImportError:
    def dupterm(x,y):
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
    _sched = False
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

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.server is not None:
            self.server.closed(self)
            self.server = None

    def start(self):
        self.sock.setsockopt(socket.SOL_SOCKET, 20, self._sched_read)
        self.send(a="h")

    def _sched_read(self, _):
        # defer to a scheduled task so that the reader isn't itself
        # interrupted when "dupterm_notify" processes a keyboard interrupt
        if not self._sched:
            self._sched = True
            schedule(self._read, _)

    def _read(self, _):
        try:
            d = self.sock.readinto(self.buf)
            if not d:
                self.close()
                return
        except Exception:
            self.close()
            raise
        else:
            self.unpacker.feed(self.buf[:d])
            for msg in self.unpacker:
                self._process(msg)
        self._sched = False

    def _process(self, msg):
        if 'a' not in msg:
            print("Not processed:",msg)
            return
        a = msg.get('a')
        i = msg.get('i',None)
        s = self.server
        try:
            cmd = getattr(s,"cmd_"+a)
        except AttributeError:
            raise UnknownCommand(a)

        try:
            if s.wdt is not None and s.wdt_any:
                s.wdt.feed()
            r = cmd(msg)
            if a == "h":
                self.server.opened(self)
            if (r is not None or i is not None) and r is not IsHandled:
                self.send('r',i=i,d=r)

        except ServerError as se:
            self.send('e',i=i,d=se.s)
        except Exception as e:
            self.send('e',i=i,d=repr(e))
            raise

    def send(self, a, d=None, **kw):
        kw["a"] = a
        if d is not None:
            kw["d"] = d
        self.sock.send(self.packer(kw))


class UFuse(IOBase):
    listen_s = None
    client = None
    clients = None  # type: set[UFuseClient]
    wdt = None
    wdt_any = False
    subs = {} # nr > (cb,topic)
    sub_nr = 1

    _fd_last = 0
    _fd_cache = None
    _repl_nr = -1
    debug_console = False
    sched = None

    def __init__(self, port=18266):
        super().__init__()

        try:
            import sched
        except ImportError:
            pass
        else:
            self.sched = sched.Scheduler()

        self.port = port
        self.clients = set()

        if RTC is not None:
            self.RTC = RTC()

        self.cons_buf = []
        self._fd_cache = dict()


    # generic calls

    # cmd_c*: REPL/console calls, see below

    def cmd_e(self, msg):
        # Error Reply
        return IsHandled

    # cmd_f*: file system calls, see below

    def cmd_h(self, msg):
        # Hello
        print("UFuse+")
        return IsHandled

    def cmd_hw(self, msg):
        # Watchdog timer
        if self.wdt:
            raise ServerError("fx")
        self.wdt = WDT(timeout=int(msg["d"]*1000))
        self.wdt_any = msg.get("p",False)

    # cmd_m*: messaging, see below.

    def cmd_p(self, msg):
        # Ping
        if self.wdt is not None and msg.get("w",False):
            self.wdt.feed()
        return msg.get('d', None)

    def cmd_r(self, msg):
        # Reply
        # right now we don't send stuff we want a reply for
        pass

    def cmd_t(self, msg):
        # get/set time: tuple(Y M D  H M S  uS)
        # right now we don't send stuff thus there's no reply to handle
        d = msg["d"]
        if RTC is None:
            raise NotImplementedError
        if d:
            self.RTC.init(d)
        else:
            return self.RTC.now()


    # REPL/Console calls

    def cmd_c(self, msg):
        # Console
        self.to_console(msg["d"])

    def cmd_ci(self, msg):
        # Console init
        self.use_console(msg.get("d",0))

    def cmd_cx(self, msg):
        # Console exit
        self.drop_console()


    # file system calls

    _fs_prefix = ""
    def _fsp(self, msg, key='d'):
        p=msg[key]
        if self._fs_prefix:
            p=self._fs_prefix+"/"+p
        if p == "":
            p = "/"
#       elif p == ".." or p.startswith("../") or "/../" in p: or p.endswith("/..")
#           raise ServerError("nf")
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

    def cmd_f_c(self, msg):
        # open, possibly chdir.
        for v in self._fd_cache.values():
            v.close()
        self._fd_cache = dict()

        d = msg.get("d")
        if not d or d == "/":
            self._fs_prefix = ""
        elif d[0] == "/":
            self._fs_prefix = d
        else:
            self._fs_prefix += "/"+d

    def cmd_fc(self, msg):
        # close
        f = self._fd(msg, drop=True)
        f.close()

    def cmd_fd(self, msg):
        # dir
        p = self._fsp(msg)
        try:
            return uos.listdir(p)
        except AttributeError:
            return [ x[0] for x in uos.ilistdir(p) ]

    def cmd_fD(self, msg):
        # new dir
        p = self._fsp(msg)
        uos.mkdir(p)

    def cmd_fg(self, msg):
        # getattr
        p = self._fsp(msg)
        try:
            s = uos.stat(p)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise ServerError("fn")
            raise
        if s[0] & 0x8000: # file
            return dict(m="f",s=s[6], t=s[7])
        elif s[0] & 0x4000: # file
            return dict(m="d", t=s[7])

    def cmd_fm(self, msg):
        # move file
        p = self._fsp(msg,'s')
        q = self._fsp(msg,'d')
        uos.stat(p)  # must exist
        if msg.get("n", False):
            # dest must not exist
            try:
                uos.stat(q)
            except OSError as err:
                if err.errno != errno.ENOENT:
                    raise
            else:
                raise ServerError("fx")
        try:
            r = self._fsp(msg,'x')
        except KeyError:
            uos.rename(p,q)
        else:
            # exchange contents, via third file
            try:
                uos.stat(r)
            except OSError as err:
                if err.errno != errno.ENOENT:
                    raise
            else:
                raise ServerError("fx")
            uos.rename(p,r)
            uos.rename(q,p)
            uos.rename(r,q)

    def cmd_fn(self, msg):
        # new file
        p = self._fsp(msg)
        f = open(p,"wb")
        f.close()

    def cmd_fo(self, msg):
        # open
        p = self._fsp(msg)
        try:
            f=open(p,msg['fm']+'b')
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise ServerError("fn")
            raise
        else:
            return self._add_f(f)

    def cmd_fr(self, msg):
        # read
        f = self._fd(msg)
        f.seek(msg['fo'])
        return f.read(msg['fs'])

    def cmd_fu(self, msg):
        # unlink
        p = self._fsp(msg)
        try:
            uos.remove(p)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise ServerError("fn")
            raise

    def cmd_fU(self, msg):
        # unlink dir
        p = self._fsp(msg)
        try:
            uos.rmdir(p)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise ServerError("fn")
            raise

    def cmd_fw(self, msg):
        # write
        f = self._fd(msg)
        f.seek(msg['fo'])
        return f.write(msg['fd'])


    # messaging

    # Callbacks get (message, *topic) as parameters.

    def cmd_m(self, msg):
        # incoming message
        self.subs[msg['p']][0](msg["d"], *msg.get("w",()))

    def cmd_ms(self, msg):
        # incoming subscription
        top = msg["d"]
        nr = msg["p"]
        self.subs[nr] = self.added_sub(nr, top)

    def cmd_mu(self, msg):
        # incoming subscription removal
        nr = msg["p"]
        del self.subs[msg["p"]]
        self.removed_sub(nr)

    def publish(self, topic, msg, raw=False, exp=()):
        if self.client:
            kw={}
            if raw:
                kw['r']=True
            if exp:
                kw['w'] = exp
            self.client.send("m", d=msg, p=topic, **kw)

    def subscribe(self, topic, cb, nr=None, raw=False):
        # Add a subscription to this topic.
        # Returns the topic number.
        if nr is None:
            nr = self.sub_nr
            self.sub_nr += 2
        self.subs[nr] = (cb, topic)
        if self.client:
            self.client.send("ms",p=nr,d=topic,r=raw)
        return nr

    def unsubscribe(self, nr):
        del self.subs[nr]
        if self.client:
            self.client.send("mu",p=nr)

    def added_sub(self, nr, topic):
        # The server added us to a subscription to this topic.
        # Must return the callback to use for messages.
        raise ServerError("no")

    def removed_sub(self, nr):
        # The server dropped us from this subscription.
        pass


    # start and stop

    def start(self):
        if self.listen_s is not None:
            raise RuntimeError("already running")
        self.listen_s = setup_conn(self.port, self._accept)

    def stop(self):
        self.drop_console()

        if self.listen_s is not None:
            self.listen_s.close()
            self.listen_s = None
        if self.client is not None:
            self.client.close()
            self.client = None
        while self.clients:
            client = self.clients.pop()
            client.close()

    def opened(self, client):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self.clients.discard(client)
        self.client = client

        for nr,ct in self.subs.items():
            if nr & 1:
                client.send(a="ms",d=ct[1],p=nr)
            # the server is responsible for the even-numbered links

    def closed(self, client):
        if self.client is client:
            self.client = None
        else:
            self.clients.discard(client)

    def to_console(self, data):
        if self._repl_nr == -1:
            print("CONS:",repr(data))
        else:
            self.cons_buf.append(data)
            if _dtn:
                _dtn(None)

    def _accept(self, sock):
        client, remote_addr = sock.accept()
        ufc = UFuseClient(self, client)
        self.clients.add(ufc)
        ufc.start()


    # console methods
    def readinto(self, buf, nbytes=-1):
        if nbytes == -1:
            nbytes = len(buf)
        if not self.cons_buf:
            return -errno.EAGAIN
        data = self.cons_buf.pop(0)
        if len(data) <= nbytes:
            buf[:len(data)] = data
            return len(data)
        else:
            buf[:nbytes] = data[0:nbytes]
            data = data[nbytes:]
            self.cons_buf.insert(0, data)
            return nbytes

    def read(self, nbytes=128):
        if not self.cons_buf:
            return -errno.EAGAIN
        data = self.cons_buf.pop(0)
        if len(data) <= nbytes:
            return data
        else:
            self.cons_buf.insert(0, data[nbytes:])
            return data[:nbytes]

    def readline(self):
        b = []
        while self.cons_buf:
            d = self.cons_buf.pop(0)
            nl = d.find(b"\n")
            if nl >= 0:
                b.append(d[:nl+1])
                d = d[nl+1:]
                if d:
                    self.cons_buf.insert(0,d)
                return b"".join(b)
            b.append(d)
        if b:
            self.cons_buf.insert(0, b"".join(b))
        return -errno.EAGAIN

    def write(self, buf):
        if self.client is not None:
            self.client.send(a="c",d=buf)

    def ioctl(self, req,flags):
        if req == 3:  # poll
            if not self.cons_buf:
                flags &=~ uselect.POLLIN
            return flags & (uselect.POLLIN|uselect.POLLOUT)
        print("IOCTL",req,flags)
        return -errno.EIO

    def use_console(self, repl_nr=0):
        if self.debug_console:
            self._repl_nr = -2
            return
        if self._repl_nr >= 0:
            return
        old = dupterm(self, repl_nr)
        self._repl_nr = repl_nr

        if old:
            old.close()

    def drop_console(self):
        rn = self._repl_nr
        if rn == -1:
            return
        self._repl_nr = -1

        if rn < 0:
            return
        c = uos.dupterm(None,rn)
        # no-op if None
        if c is not self:
            # owch. restore
            uos.dupterm(c, rn)
            raise ServerError("cx")


    def close(self):
        # Console closed. Called by whoever replaces our dupterm entry.
        if self._repl_nr == -1:
            return
        self._repl_nr = -1
        if self.client:
            self.client.send(a="cx")


