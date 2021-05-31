# coding: utf-8
# copied from https://github.com/msgpack/msgpack-python
from collections import namedtuple
import uos as os
import usys as sys
import struct
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

newlist_hint = lambda size: []

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
    unpacker = Unpacker(None, max_buffer_size=len(packed), **kwargs)
    unpacker.feed(packed)
    try:
        ret = unpacker._unpack()
    except OutOfData:
        raise ValueError("Unpack failed: incomplete input")
    if unpacker._got_extradata():
        raise ExtraData(ret, unpacker._get_extradata())
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
        file_like=None,
        read_size=0,
        use_list=True,
        raw=False,
        timestamp=0,
        strict_map_key=True,
        object_hook=None,
        object_pairs_hook=None,
        list_hook=None,
        unicode_errors=None,
        max_buffer_size=10 * 1024,
        ext_hook=ExtType,
        max_str_len=-1,
        max_bin_len=-1,
        max_array_len=-1,
        max_map_len=-1,
        max_ext_len=-1,
    ):
        if unicode_errors is None:
            unicode_errors = "strict"

        if file_like is None:
            self._feeding = True
        else:
            if not callable(file_like.read):
                raise TypeError("`file_like.read` must be callable")
            self.file_like = file_like
            self._feeding = False

        #: array of bytes fed.
        self._buffer = bytearray()
        #: Which position we currently reads
        self._buff_i = 0

        self._buf_checkpoint = 0

        if not max_buffer_size:
            max_buffer_size = 2 ** 31 - 1
        if max_str_len == -1:
            max_str_len = max_buffer_size
        if max_bin_len == -1:
            max_bin_len = max_buffer_size
        if max_array_len == -1:
            max_array_len = max_buffer_size
        if max_map_len == -1:
            max_map_len = max_buffer_size // 2
        if max_ext_len == -1:
            max_ext_len = max_buffer_size

        self._max_buffer_size = max_buffer_size
        if read_size > self._max_buffer_size:
            raise ValueError("read_size must be smaller than max_buffer_size")
        self._read_size = read_size or min(self._max_buffer_size, 16 * 1024)
        self._raw = bool(raw)
        self._strict_map_key = bool(strict_map_key)
        self._unicode_errors = unicode_errors
        self._use_list = use_list
        if not (0 <= timestamp <= 3):
            raise ValueError("timestamp must be 0..3")
        self._timestamp = timestamp
        self._list_hook = list_hook
        self._object_hook = object_hook
        self._object_pairs_hook = object_pairs_hook
        self._ext_hook = ext_hook
        self._max_str_len = max_str_len
        self._max_bin_len = max_bin_len
        self._max_array_len = max_array_len
        self._max_map_len = max_map_len
        self._max_ext_len = max_ext_len
        self._stream_offset = 0

        if list_hook is not None and not callable(list_hook):
            raise TypeError("`list_hook` is not callable")
        if object_hook is not None and not callable(object_hook):
            raise TypeError("`object_hook` is not callable")
        if object_pairs_hook is not None and not callable(object_pairs_hook):
            raise TypeError("`object_pairs_hook` is not callable")
        if object_hook is not None and object_pairs_hook is not None:
            raise TypeError(
                "object_pairs_hook and object_hook are mutually " "exclusive"
            )
        if not callable(ext_hook):
            raise TypeError("`ext_hook` is not callable")

    def feed(self, next_bytes):
        assert self._feeding
        view = _get_data_from_buffer(next_bytes)
        if len(self._buffer) - self._buff_i + len(view) > self._max_buffer_size:
            raise BufferFull

        # Strip buffer before checkpoint before reading file.
        if self._buf_checkpoint > 0:
            # del self._buffer[: self._buf_checkpoint]
            self._buffer = self._buffer[self._buf_checkpoint :]
            self._buff_i -= self._buf_checkpoint
            self._buf_checkpoint = 0

        # Use extend here: INPLACE_ADD += doesn't reliably typecast memoryview in jython
        self._buffer.extend(view)

    def _consume(self):
        """ Gets rid of the used parts of the buffer. """
        self._stream_offset += self._buff_i - self._buf_checkpoint
        self._buf_checkpoint = self._buff_i

    def _got_extradata(self):
        return self._buff_i < len(self._buffer)

    def _get_extradata(self):
        return self._buffer[self._buff_i :]

    def read_bytes(self, n):
        ret = self._read(n, raise_outofdata=False)
        self._consume()
        return ret

    def _read(self, n, raise_outofdata=True):
        # (int) -> bytearray
        self._reserve(n, raise_outofdata=raise_outofdata)
        i = self._buff_i
        ret = self._buffer[i : i + n]
        self._buff_i = i + len(ret)
        return ret

    def _reserve(self, n, raise_outofdata=True):
        remain_bytes = len(self._buffer) - self._buff_i - n

        # Fast path: buffer has n bytes already
        if remain_bytes >= 0:
            return

        if self._feeding:
            self._buff_i = self._buf_checkpoint
            raise OutOfData

        # Strip buffer before checkpoint before reading file.
        if self._buf_checkpoint > 0:
            # del self._buffer[: self._buf_checkpoint]
            self._buffer = self._buffer[self._buf_checkpoint :]
            self._buff_i -= self._buf_checkpoint
            self._buf_checkpoint = 0

        # Read from file
        remain_bytes = -remain_bytes
        while remain_bytes > 0:
            to_read_bytes = max(self._read_size, remain_bytes)
            read_data = self.file_like.read(to_read_bytes)
            if not read_data:
                break
            assert isinstance(read_data, bytes)
            self._buffer += read_data
            remain_bytes -= len(read_data)

        if len(self._buffer) < n + self._buff_i and raise_outofdata:
            self._buff_i = 0  # rollback
            raise OutOfData

    def _read_header(self):
        typ = TYPE_IMMEDIATE
        n = 0
        obj = None
        self._reserve(1)
        b = self._buffer[self._buff_i]
        self._buff_i += 1
        if b & 0b10000000 == 0:  # x00-x7F
            obj = b
        elif b & 0b11100000 == 0b11100000:  # xE0-xFF
            obj = -1 - (b ^ 0xFF)
        elif b & 0b11100000 == 0b10100000:  # xA0-xBF
            n = b & 0b00011111
            typ = TYPE_RAW
            if n > self._max_str_len:
                raise ValueError("%s exceeds max_str_len(%s)" % (n, self._max_str_len))
            obj = self._read(n)
        elif b & 0b11110000 == 0b10010000:  # x90-x9F
            n = b & 0b00001111
            typ = TYPE_ARRAY
            if n > self._max_array_len:
                raise ValueError(
                    "%s exceeds max_array_len(%s)" % (n, self._max_array_len)
                )
        elif b & 0b11110000 == 0b10000000:  # x80-x8F
            n = b & 0b00001111
            typ = TYPE_MAP
            if n > self._max_map_len:
                raise ValueError("%s exceeds max_map_len(%s)" % (n, self._max_map_len))
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
            self._reserve(size)
            if len(fmt) > 0:
                n = struct.unpack_from(fmt, self._buffer, self._buff_i)[0]
            else:
                n = self._buffer[self._buff_i]
            self._buff_i += size
            if n > self._max_bin_len:
                raise ValueError("%s exceeds max_bin_len(%s)" % (n, self._max_bin_len))
            obj = self._read(n)
        elif b <= 0xC9:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            self._reserve(size)
            L, n = struct.unpack_from(fmt, self._buffer, self._buff_i)
            self._buff_i += size
            if L > self._max_ext_len:
                raise ValueError("%s exceeds max_ext_len(%s)" % (L, self._max_ext_len))
            obj = self._read(L)
        elif b <= 0xD3:
            size, fmt = _MSGPACK_HEADERS[b]
            self._reserve(size)
            if len(fmt) > 0:
                obj = struct.unpack_from(fmt, self._buffer, self._buff_i)[0]
            else:
                obj = self._buffer[self._buff_i]
            self._buff_i += size
        elif b <= 0xD8:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            if self._max_ext_len < size:
                raise ValueError(
                    "%s exceeds max_ext_len(%s)" % (size, self._max_ext_len)
                )
            self._reserve(size + 1)
            n, obj = struct.unpack_from(fmt, self._buffer, self._buff_i)
            self._buff_i += size + 1
        elif b <= 0xDB:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            self._reserve(size)
            if len(fmt) > 0:
                (n,) = struct.unpack_from(fmt, self._buffer, self._buff_i)
            else:
                n = self._buffer[self._buff_i]
            self._buff_i += size
            if n > self._max_str_len:
                raise ValueError("%s exceeds max_str_len(%s)" % (n, self._max_str_len))
            obj = self._read(n)
        elif b <= 0xDD:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            self._reserve(size)
            (n,) = struct.unpack_from(fmt, self._buffer, self._buff_i)
            self._buff_i += size
            if n > self._max_array_len:
                raise ValueError(
                    "%s exceeds max_array_len(%s)" % (n, self._max_array_len)
                )
        elif b <= 0xDF:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            self._reserve(size)
            (n,) = struct.unpack_from(fmt, self._buffer, self._buff_i)
            self._buff_i += size
            if n > self._max_map_len:
                raise ValueError("%s exceeds max_map_len(%s)" % (n, self._max_map_len))
        else:
            raise FormatError("Unknown header: 0x%x" % b)
        return typ, n, obj

    def _unpack(self):
        typ, n, obj = self._read_header()

        # TODO should we eliminate the recursion?
        if typ == TYPE_ARRAY:
            ret = newlist_hint(n)
            for i in range(n):
                ret.append(self._unpack())
            if self._list_hook is not None:
                ret = self._list_hook(ret)
            # TODO is the interaction between `list_hook` and `use_list` ok?
            return ret if self._use_list else tuple(ret)
        if typ == TYPE_MAP:
            if self._object_pairs_hook is not None:
                ret = self._object_pairs_hook(
                    (self._unpack(), self._unpack())
                    for _ in range(n)
                )
            else:
                ret = {}
                for _ in range(n):
                    key = self._unpack()
                    if self._strict_map_key and type(key) not in (str, bytes):
                        raise ValueError(
                            "%s is not allowed for map key" % str(type(key))
                        )
                    if type(key) is str and hasattr(sys,'intern'):
                        key = sys.intern(key)
                    ret[key] = self._unpack()
                if self._object_hook is not None:
                    ret = self._object_hook(ret)
            return ret
        if typ == TYPE_RAW:
            if self._raw:
                obj = bytes(obj)
            else:
                obj = obj.decode("utf_8", self._unicode_errors)
            return obj
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
            self._consume()
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
        use_single_float=False,
        autoreset=True,
        use_bin_type=True,
        strict_types=False,
        unicode_errors=None,
    ):
        self._strict_types = strict_types
        self._use_float = use_single_float
        self._autoreset = autoreset
        self._use_bin_type = use_bin_type
        self._buffer = BytesIO()
        self._unicode_errors = unicode_errors or "strict"
        if default is not None:
            if not callable(default):
                raise TypeError("default must be callable")
        self._default = default

    def _pack(
        self,
        obj,
        nest_limit=DEFAULT_RECURSE_LIMIT,
        check=isinstance,
        check_type_strict=_check_type_strict,
    ):
        default_used = False
        if self._strict_types:
            check = check_type_strict
            list_types = list
        else:
            list_types = (list, tuple)

        # shorter bytecode
        def wp(*x):
            return self._buffer.write(struct.pack(*x))
        wb = self._buffer.write
        while True:
            if nest_limit < 0:
                raise ValueError("recursion limit exceeded")
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
                if n >= 2 ** 32:
                    raise ValueError("%s is too large" % type(obj).__name__)
                self._pack_bin_header(n)
                return wb(obj)
            if check(obj, str):
                obj = obj.encode("utf-8", self._unicode_errors)
                n = len(obj)
                if n >= 2 ** 32:
                    raise ValueError("String is too large")
                self._pack_raw_header(n)
                return wb(obj)
            if check(obj, memoryview):
                n = len(obj) * obj.itemsize
                if n >= 2 ** 32:
                    raise ValueError("Memoryview is too large")
                self._pack_bin_header(n)
                return wb(obj)
            if check(obj, float):
                if self._use_float:
                    return wp(">Bf", 0xCA, obj)
                return wp(">Bd", 0xCB, obj)
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
                    self._pack(obj[i], nest_limit - 1)
                return
            if check(obj, dict):
                return self._pack_map_pairs(
                    len(obj), obj.items(), nest_limit - 1
                )

            if not default_used and self._default is not None:
                obj = self._default(obj)
                default_used = 1
                continue
            raise TypeError("Cannot serialize %r" % (obj,))

    def pack(self, obj):
        try:
            self._pack(obj)
        except:
            self._buffer = BytesIO()  # force reset
            raise
        if self._autoreset:
            ret = self._buffer.getvalue()
            self._buffer = BytesIO()
            return ret

    def pack_map_pairs(self, pairs):
        self._pack_map_pairs(len(pairs), pairs)
        if self._autoreset:
            ret = self._buffer.getvalue()
            self._buffer = BytesIO()
            return ret

    def pack_array_header(self, n):
        if n >= 2 ** 32:
            raise ValueError
        self._pack_array_header(n)
        if self._autoreset:
            ret = self._buffer.getvalue()
            self._buffer = BytesIO()
            return ret

    def pack_map_header(self, n):
        if n >= 2 ** 32:
            raise ValueError
        self._pack_map_header(n)
        if self._autoreset:
            ret = self._buffer.getvalue()
            self._buffer = BytesIO()
            return ret

    def pack_ext_type(self, typecode, data):
        if not isinstance(typecode, int):
            raise TypeError("typecode must have int type.")
        if not 0 <= typecode <= 127:
            raise ValueError("typecode should be 0-127")
        if not isinstance(data, bytes):
            raise TypeError("data must have bytes type")
        L = len(data)
        if L > 0xFFFFFFFF:
            raise ValueError("Too large data")
        if L == 1:
            self._buffer.write(b"\xd4")
        elif L == 2:
            self._buffer.write(b"\xd5")
        elif L == 4:
            self._buffer.write(b"\xd6")
        elif L == 8:
            self._buffer.write(b"\xd7")
        elif L == 16:
            self._buffer.write(b"\xd8")
        elif L <= 0xFF:
            self._buffer.write(b"\xc7" + struct.pack("B", L))
        elif L <= 0xFFFF:
            self._buffer.write(b"\xc8" + struct.pack(">H", L))
        else:
            self._buffer.write(b"\xc9" + struct.pack(">I", L))
        self._buffer.write(struct.pack("B", typecode))
        self._buffer.write(data)

    def _pack_array_header(self, n):
        if n <= 0x0F:
            return self._buffer.write(struct.pack("B", 0x90 + n))
        if n <= 0xFFFF:
            return self._buffer.write(struct.pack(">BH", 0xDC, n))
        if n <= 0xFFFFFFFF:
            return self._buffer.write(struct.pack(">BI", 0xDD, n))
        raise ValueError("Array is too large")

    def _pack_map_header(self, n):
        if n <= 0x0F:
            return self._buffer.write(struct.pack("B", 0x80 + n))
        if n <= 0xFFFF:
            return self._buffer.write(struct.pack(">BH", 0xDE, n))
        if n <= 0xFFFFFFFF:
            return self._buffer.write(struct.pack(">BI", 0xDF, n))
        raise ValueError("Dict is too large")

    def _pack_map_pairs(self, n, pairs, nest_limit=DEFAULT_RECURSE_LIMIT):
        self._pack_map_header(n)
        for (k, v) in pairs:
            self._pack(k, nest_limit - 1)
            self._pack(v, nest_limit - 1)

    def _pack_raw_header(self, n):
        if n <= 0x1F:
            self._buffer.write(struct.pack("B", 0xA0 + n))
        elif self._use_bin_type and n <= 0xFF:
            self._buffer.write(struct.pack(">BB", 0xD9, n))
        elif n <= 0xFFFF:
            self._buffer.write(struct.pack(">BH", 0xDA, n))
        elif n <= 0xFFFFFFFF:
            self._buffer.write(struct.pack(">BI", 0xDB, n))
        else:
            raise ValueError("Raw is too large")

    def _pack_bin_header(self, n):
        if not self._use_bin_type:
            return self._pack_raw_header(n)
        elif n <= 0xFF:
            return self._buffer.write(struct.pack(">BB", 0xC4, n))
        elif n <= 0xFFFF:
            return self._buffer.write(struct.pack(">BH", 0xC5, n))
        elif n <= 0xFFFFFFFF:
            return self._buffer.write(struct.pack(">BI", 0xC6, n))
        else:
            raise ValueError("Bin is too large")

    def bytes(self):
        """Return internal buffer contents as bytes object"""
        return self._buffer.getvalue()

    def reset(self):
        """Reset internal buffer.

        This method is useful only when autoreset=False.
        """
        self._buffer = BytesIO()

    def getbuffer(self):
        """Return view of internal buffer."""
        return self._buffer.getbuffer()

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

