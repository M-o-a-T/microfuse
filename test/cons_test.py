# micropython

from uio import IOBase
import errno
import uselect

class Console(IOBase):
    def __init__(self):
        self.cons_wbuf = []

    def ioctl(self, req,flags):
        if req == 3:  # poll
            return flags & uselect.POLLOUT
        return -errno.EIO

    def readinto(self, buf, nbytes=-1):
        return -errno.EAGAIN
    def readline(self):
        return -errno.EAGAIN
    def read(self, nbytes=-1):
        return -errno.EAGAIN

    def write(self, buf):
        self.cons_wbuf.append(str(buf))

