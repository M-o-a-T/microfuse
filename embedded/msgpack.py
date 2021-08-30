# coding: utf-8
# copied from https://github.com/msgpack/msgpack-python
import struct
from collections import namedtuple

import uos as os
import usys as sys
from uio import BytesIO


class UnpackException(Exception):
    pass


class BufferFull(UnpackException):
    pass


class OutOfData(UnpackException):
    pass


class FormatError(UnpackException):
    pass


class StackError(UnpackException):
    pass


# Deprecated.  Use ValueError instead
UnpackValueError = ValueError


class ExtraData(UnpackValueError):
    def __init__(self, unpacked, extra):
        self.unpacked = unpacked
        self.extra = extra

    def __str__(self):
        return "unpack(b) received extra data."


class ExtType(namedtuple("ExtType", "code data")):
    """ExtType represents ext type in msgpack."""

    def __new__(cls, code, data):
        if not isinstance(code, int):
            raise TypeError("code must be int")
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        if not 0 <= code <= 127:
            raise ValueError("code must be 0~127")
        return super(ExtType, cls).__new__(cls, code, data)

TYPE_IMMEDIATE = 0
TYPE_ARRAY = 1
TYPE_MAP = 2
TYPE_RAW = 3
TYPE_BIN = 4
TYPE_EXT = 5

DEFAULT_RECURSE_LIMIT = 20


def _check_type_strict(obj, t, type=type, tuple=tuple):
    if type(t) is tuple:
        return type(obj) in t
    else:
        return type(obj) is t


def _get_data_from_buffer(obj):
    view = memoryview(obj)
    return view


def unpackb(packed, **kwargs):
    """
    Unpack an object from `packed`.

    Raises ``ExtraData`` when *packed* contains extra bytes.
    Raises ``ValueError`` when *packed* is incomplete.
    Raises ``FormatError`` when *packed* is not valid msgpack.
    Raises ``StackError`` when *packed* contains too nested.
    Other exceptions can be raised during unpacking.

    See :class:`Unpacker` for options.
    """
    unpacker = Unpacker(None, buffer_size=len(packed), **kwargs)
    unpacker.feed(packed)
    try:
        ret = unpacker._unpack()
    except OutOfData:
        raise ValueError("incomplete")
    if unpacker._has_extra():
        raise ExtraData(ret, unpacker._get_extra())
    return ret


_NO_FORMAT_USED = ""
_MSGPACK_HEADERS = {
    0xC4: (1, _NO_FORMAT_USED, TYPE_BIN),
    0xC5: (2, ">H", TYPE_BIN),
    0xC6: (4, ">I", TYPE_BIN),
    0xC7: (2, "Bb", TYPE_EXT),
    0xC8: (3, ">Hb", TYPE_EXT),
    0xC9: (5, ">Ib", TYPE_EXT),
    0xCA: (4, ">f"),
    0xCB: (8, ">d"),
    0xCC: (1, _NO_FORMAT_USED),
    0xCD: (2, ">H"),
    0xCE: (4, ">I"),
    0xCF: (8, ">Q"),
    0xD0: (1, "b"),
    0xD1: (2, ">h"),
    0xD2: (4, ">i"),
    0xD3: (8, ">q"),
    0xD4: (1, "b1s", TYPE_EXT),
    0xD5: (2, "b2s", TYPE_EXT),
    0xD6: (4, "b4s", TYPE_EXT),
    0xD7: (8, "b8s", TYPE_EXT),
    0xD8: (16, "b16s", TYPE_EXT),
    0xD9: (1, _NO_FORMAT_USED, TYPE_RAW),
    0xDA: (2, ">H", TYPE_RAW),
    0xDB: (4, ">I", TYPE_RAW),
    0xDC: (2, ">H", TYPE_ARRAY),
    0xDD: (4, ">I", TYPE_ARRAY),
    0xDE: (2, ">H", TYPE_MAP),
    0xDF: (4, ">I", TYPE_MAP),
}


class Unpacker(object):
    def __init__(
        self,
        reader,  # readinto()
        buf_size=5*1024,  # max message size
        as_memview=32,  # bytestrings at least that size are returned as memoryview
        ext_hook=ExtType,
    ):
        self.reader = reader

        #: Input buffer. Must be at least max expected packet.
        self._buffer = bytearray(buf_size)
        self._b = memoryview(self._buffer)
        self._b_l = buf_size

        #: Which position we currently read
        self._b_i = 0

        #: buffer fill mark
        self._b_n = 0

        #: start of current message
        self._b_cp = 0

        self._mvm = as_memview
        self._ext_hook = ext_hook
        self._stream_offset = 0

    def read(self):
        # returns #bytes
        if self._b_i == 0:
            if self._b_n == self._b_l:
                # buffer full
                raise ValueError("msg too long")
        elif self._b_n == self._b_l:
            # buffer at end, move data to buffer start
            n = self._b_n - self._b_i
            self._b[:n] = self._b[-n:]
            self._b_n = n
            self._b_i = 0

        n = self.reader(self._b[self._b_n:])
        self._b_n += n
        return n


    def _consume(self):
        # a packet is done
        if self._b_i == self._b_n:
            self._b_i = self._b_n = 0

        self._b_cp = self._b_i

    def _has_extra(self):
        return self._b_i < self._b_n

    def _get_extra(self):
        return self._b[self._b_i:self._b_n]

    def _read(self, n):
        # (int) -> bytearray
        self._has(n)
        i = self._b_i
        j = i + n
        ret = self._b[i : j]
        if self._mvm < n:
            ret = bytearray(ret)
        self._b_i = j
        return ret

    def _has(self, n):
        remain_bytes = self._b_n - self._b_i - n

        # Fast path: buffer has n bytes already
        if remain_bytes >= 0:
            return

        self._b_i = self._b_cp
        raise OutOfData

    def _hdr(self):
        typ = TYPE_IMMEDIATE
        n = 0
        obj = None
        self._has(1)
        b = self._b[self._b_i]
        self._b_i += 1
        if b & 0b10000000 == 0:  # x00-x7F
            obj = b
        elif b & 0b11100000 == 0b11100000:  # xE0-xFF
            obj = -1 - (b ^ 0xFF)
        elif b & 0b11100000 == 0b10100000:  # xA0-xBF
            n = b & 0b00011111
            typ = TYPE_RAW
            obj = self._read(n)
        elif b & 0b11110000 == 0b10010000:  # x90-x9F
            n = b & 0b00001111
            typ = TYPE_ARRAY
        elif b & 0b11110000 == 0b10000000:  # x80-x8F
            n = b & 0b00001111
            typ = TYPE_MAP
        elif b == 0xC0:
            obj = None
        elif b == 0xC1:
             raise RuntimeError("unused code")
        elif b == 0xC2:
            obj = False
        elif b == 0xC3:
            obj = True
        elif b <= 0xC6:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            self._has(size)
            if len(fmt) > 0:
                n = struct.unpack_from(fmt, self._b, self._b_i)[0]
            else:
                n = self._b[self._b_i]
            self._b_i += size
            obj = self._read(n)
        elif b <= 0xC9:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            self._has(size)
            L, n = struct.unpack_from(fmt, self._b, self._b_i)
            self._b_i += size
            obj = self._read(L)
        elif b <= 0xD3:
            size, fmt = _MSGPACK_HEADERS[b]
            self._has(size)
            if len(fmt) > 0:
                obj = struct.unpack_from(fmt, self._b, self._b_i)[0]
            else:
                obj = self._b[self._b_i]
            self._b_i += size
        elif b <= 0xD8:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            self._has(size + 1)
            n, obj = struct.unpack_from(fmt, self._b, self._b_i)
            self._b_i += size + 1
        elif b <= 0xDB:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            self._has(size)
            if len(fmt) > 0:
                (n,) = struct.unpack_from(fmt, self._b, self._b_i)
            else:
                n = self._b[self._b_i]
            self._b_i += size
            obj = self._read(n)
        elif b <= 0xDD:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            self._has(size)
            (n,) = struct.unpack_from(fmt, self._b, self._b_i)
            self._b_i += size
        elif b <= 0xDF:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            self._has(size)
            (n,) = struct.unpack_from(fmt, self._b, self._b_i)
            self._b_i += size
        else:
            raise RuntimeError("?!?")
        return typ, n, obj

    def _unpack(self):
        typ, n, obj = self._hdr()

        if typ == TYPE_ARRAY:
            ret = [None]*n
            for i in range(n):
                ret[i] = self._unpack()
            return ret
        if typ == TYPE_MAP:
            ret = {}
            for _ in range(n):
                key = self._unpack()
                ret[key] = self._unpack()
            return ret
        if typ == TYPE_RAW:
            return str(obj, "utf_8")
        if typ == TYPE_BIN:
            return bytes(obj)
        if typ == TYPE_EXT:
            return self._ext_hook(n, bytes(obj))
        assert typ == TYPE_IMMEDIATE
        return obj

    def __iter__(self):
        return self

    def __next__(self):
        try:
            ret = self._unpack()
            self._consume()
            return ret
        except OutOfData:
            raise StopIteration

    next = __next__

    def unpack(self):
        ret = self._unpack()
        self._consume()
        return ret


class Packer(object):
    def __init__(
        self,
        default=None,
        buf_len=64,
        writer=None,
    ):
        self._b_l = buf_len
        self._buffer = bytearray(buf_len)
        self._b = memoryview(self._buffer)
        self._b_p = 0
        self.writer = writer

        self._default = default
        self._bad = False

    def _wb(self, x):
        # write bytes or similar
        lx = len(x)
        if self._b_p + lx <= self._b_l:
            # fits into the buffer
            self._b[self._b_p:self._b_p+lx] = x
            self._b_p += lx
            self.flush()
            return
        # doesn't.
        self.flush()

        if lx*2 >= self._b_l:
            # at least half the buffer: write directly
            self.writer(x)
            return
        self._b[:lx] = x
        self._b_p = lx

    def _wp(self, fmt, *x):
        # write pack
        try:
            struct.pack_into(fmt,self._b,self._b_p, *x)
            self._b_p += struct.calcsize(fmt)
        except ValueError:
            self.flush()
            try:
                struct.pack_into(fmt,self._b,0, *x)
                self._b_p = struct.calcsize(fmt)
            except ValueError:
                # if we already wrote an incomplete buffer we're SOL
                self._b_p = 0
                raise

    def _pack(
        self,
        obj,
        check=isinstance,
        check_type_strict=_check_type_strict,
    ):
        default_used = False
        list_types = (list, tuple)

        wb = self._wb
        wp = self._wp

        while True:
            if obj is None:
                return wb(b"\xc0")
            if check(obj, bool):
                if obj:
                    return wb(b"\xc3")
                return wb(b"\xc2")
            if check(obj, int):
                if obj >= 0:
                    if obj < 0x80:
                        return wp("B", obj)
                    if obj <= 0xFF:
                        return wp("BB", 0xCC, obj)
                    if obj <= 0xFFFF:
                        return wp(">BH", 0xCD, obj)
                    if obj <= 0xFFFFFFFF:
                        return wp(">BI", 0xCE, obj)
                    if 0xFFFFFFFF < obj <= 0xFFFFFFFFFFFFFFFF:
                        return wp(">BQ", 0xCF, obj)
                else:
                    if -0x20 <= obj:
                        return wp("b", obj)
                    if -0x80 <= obj:
                        return wp(">Bb", 0xD0, obj)
                    if -0x8000 <= obj:
                        return wp(">Bh", 0xD1, obj)
                    if -0x80000000 <= obj:
                        return wp(">Bi", 0xD2, obj)
                    if -0x8000000000000000 <= obj:
                        return wp(">Bq", 0xD3, obj)
                if not default_used and self._default is not None:
                    obj = self._default(obj)
                    default_used = True
                    continue
                raise OverflowError("Integer value out of range")
            if check(obj, (bytes, bytearray)):
                n = len(obj)
                self._pack_bin_header(n)
                return wb(obj)
            if check(obj, str):
                obj = obj.encode("utf-8")
                n = len(obj)
                self._pack_raw_header(n)
                return wb(obj)
            if check(obj, memoryview):
                n = len(obj) * obj.itemsize
                self._pack_bin_header(n)
                return wb(obj)
            if check(obj, float):
                return wp(">Bf", 0xCA, obj)
            if check(obj, ExtType):
                code = obj.code
                data = obj.data
                assert isinstance(code, int)
                assert isinstance(data, bytes)
                L = len(data)
                if L == 1:
                    wb(b"\xd4")
                elif L == 2:
                    wb(b"\xd5")
                elif L == 4:
                    wb(b"\xd6")
                elif L == 8:
                    wb(b"\xd7")
                elif L == 16:
                    wb(b"\xd8")
                elif L <= 0xFF:
                    wp(">BB", 0xC7, L)
                elif L <= 0xFFFF:
                    wp(">BH", 0xC8, L)
                else:
                    wp(">BI", 0xC9, L)
                wp("b", code)
                wb(data)
                return
            if check(obj, list_types):
                n = len(obj)
                self._pack_array_header(n)
                for i in range(n):
                    self._pack(obj[i])
                return
            if check(obj, dict):
                return self._pack_map_pairs(len(obj), obj.items())

            if not default_used and self._default is not None:
                obj = self._default(obj)
                default_used = 1
                continue
            raise TypeError("Cannot serialize %r" % (obj,))

    def pack(self, obj, keep=False):
        # errors are fatal
        if self._bad:
            raise ValueError("inconsistent")
        try:
            self._bad = True
            self._pack(obj)
            if not keep:
                self.flush()
            self._bad = False
        except Exception as e:
            print("ERR",e)
            raise

    def flush(self):
        if self._b_p > 0:
            self.writer(self._b[:self._b_p])
        self._b_p = 0

    def pack_map_pairs(self, pairs):
        self._pack_map_pairs(len(pairs), pairs)

    def pack_array_header(self, n):
        self._pack_array_header(n)

    def pack_map_header(self, n):
        if n >= 2 ** 32:
            raise ValueError
        self._pack_map_header(n)

    def pack_ext_type(self, typecode, data):
        if not isinstance(typecode, int):
            raise TypeError("typecode must have int type.")
        if not 0 <= typecode <= 127:
            raise ValueError("typecode should be 0-127")
        if not isinstance(data, bytes):
            raise TypeError("data must have bytes type")
        L = len(data)
        if L == 1:
            self._wb(b"\xd4")
        elif L == 2:
            self._wb(b"\xd5")
        elif L == 4:
            self._wb(b"\xd6")
        elif L == 8:
            self._wb(b"\xd7")
        elif L == 16:
            self._wb(b"\xd8")
        elif L <= 0xFF:
            self._wb(b"\xc7" + struct.pack("B", L))
        elif L <= 0xFFFF:
            self._wb(b"\xc8" + struct.pack(">H", L))
        else:
            self._wb(b"\xc9" + struct.pack(">I", L))
        self._wb(struct.pack("B", typecode))
        self._wb(data)

    def _pack_array_header(self, n):
        if n <= 0x0F:
            return self._wp("B", 0x90 + n)
        elif n <= 0xFFFF:
            return self._wp(">BH", 0xDC, n)
        else:
            return self._wp(">BI", 0xDD, n)

    def _pack_map_header(self, n):
        if n <= 0x0F:
            return self._wp("B", 0x80 + n)
        elif n <= 0xFFFF:
            return self._wp(">BH", 0xDE, n)
        else:
            return self._wp(">BI", 0xDF, n)

    def _pack_map_pairs(self, n, pairs):
        self._pack_map_header(n)
        for k, v in pairs:
            self._pack(k)
            self._pack(v)

    def _pack_raw_header(self, n):
        if n <= 0x1F:
            self._wp("B", 0xA0 + n)
        elif n <= 0xFF:
            self._wp(">BB", 0xD9, n)
        elif n <= 0xFFFF:
            self._wp(">BH", 0xDA, n)
        else:
            self._wp(">BI", 0xDB, n)

    def _pack_bin_header(self, n):
        if n <= 0xFF:
            return self._wp(">BB", 0xC4, n)
        elif n <= 0xFFFF:
            return self._wp(">BH", 0xC5, n)
        else:
            return self._wp(">BI", 0xC6, n)

    def reset(self):
        """Reset internal buffer.
        """
        self._b_i = self._b_n = 0
        self._bad = False

def pack(o, stream, **kwargs):
    """
    Pack object `o` and write it to `stream`

    See :class:`Packer` for options.
    """
    packer = Packer(**kwargs)
    stream.write(packer.pack(o))


def packb(o, **kwargs):
    """
    Pack object `o` and return packed bytes

    See :class:`Packer` for options.
    """
    return Packer(**kwargs).pack(o)


def unpack(stream, **kwargs):
    """
    Unpack an object from `stream`.

    Raises `ExtraData` when `stream` contains extra bytes.
    See :class:`Unpacker` for options.
    """
    data = stream.read()
    return unpackb(data, **kwargs)

