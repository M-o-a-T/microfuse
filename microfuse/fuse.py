# FUSE operations

import pyfuse3
from pyfuse3 import FUSEError, EntryAttributes, FileInfo
import errno
import os
import stat
from pathlib import PosixPath as Path
from .link import ServerError
from collections import defaultdict
import logging
logger = logging.getLogger(__name__)

class Operations(pyfuse3.Operations):
    supports_dot_lookup = False
    enable_writeback_cache = False
    enable_acl = False

    max_read = 256

    def __init__(self, link):
        super().__init__()
        self._inode_path_map = { pyfuse3.ROOT_INODE: Path("/") }
        self._path_inode_map = { Path("/"): pyfuse3.ROOT_INODE }
        self._lookup_cnt = defaultdict(lambda : 0)
        self._fd_inode_map = dict()
        self._inode_fd_map = dict()
        self._fd_open_count = dict()

        self._dir_content = dict()
        self._last_dir_fh = 0

        self._link = link
        self._last_inode = pyfuse3.ROOT_INODE

    def f_open(self, fd, inode):
        self._fd_inode_map[fd] = inode
        self._inode_fd_map[inode] = fd

    def f_close(self, fd):
        i = self._fd_inode_map.pop(fd)
        del self._inode_fd_map[i]

    def i_path(self, inode):
        try:
            return self._inode_path_map[inode]
        except KeyError:
            logger.debug("NotFound: i=%r", inode)
            raise FUSEError(errno.ENOENT)

    def i_add(self, path, inode=None):
        try:
            return self._path_inode_map[path]
        except KeyError:
            if inode is None:
                self._last_inode += 1
                inode = self._last_inode
            self._inode_path_map[inode] = path
            self._path_inode_map[path] = inode
            return inode

    def i_del(self, inode):
        try:
            p = self._inode_path_map.pop(inode)
        except KeyError:
            pass
        else:
            del self._path_inode_map[p]

    def raise_error(self, err, inode=None):
        if err.s == "nf":
            if inode is not None:
                self.i_del(inode)
            raise FUSEError(errno.ENOENT)
        raise FUSEError(errno.EIO)

    async def lookup(self, parent_inode, name, ctx):
        '''Look up a directory entry by name and get its attributes.

        This method should return an `EntryAttributes` instance for the
        directory entry *name* in the directory with inode *parent_inode*.

        If there is no such entry, the method should either return an
        `EntryAttributes` instance with zero ``st_ino`` value (in which case
        the negative lookup will be cached as specified by ``entry_timeout``),
        or it should raise `FUSEError` with an errno of `errno.ENOENT` (in this
        case the negative result will not be cached).

        *ctx* will be a `RequestContext` instance.

        The file system must be able to handle lookups for :file:`.` and
        :file:`..`, no matter if these entries are returned by `readdir` or not.

        (Successful) execution of this handler increases the lookup count for
        the returned inode by one.
        '''
        p = self.i_path(parent_inode) / name.decode("utf-8")
        try:
            return await self.getattr(self._path_inode_map[p], ctx)
        except KeyError:
            try:
                d = await self._link.send("fg",str(p))
            except ServerError as err:
                self.raise_error(err)
            else:
                inode = self.i_add(p)
                return await self.getattr(inode, ctx, _res=d)


    async def forget(self, inode_list):
        '''Decrease lookup counts for inodes in *inode_list*

        *inode_list* is a list of ``(inode, nlookup)`` tuples. This method
        should reduce the lookup count for each *inode* by *nlookup*.

        If the lookup count reaches zero, the inode is currently not known to
        the kernel. In this case, the file system will typically check if there
        are still directory entries referring to this inode and, if not, remove
        the inode.

        If the file system is unmounted, it may not have received `forget` calls
        to bring all lookup counts to zero. The filesystem needs to take care to
        clean up inodes that at that point still have non-zero lookup count
        (e.g. by explicitly calling `forget` with the current lookup count for
        every such inode after `main` has returned).

        This method must not raise any exceptions (not even `FUSEError`), since
        it is not handling a particular client request.
        '''

        for i in inode_list:
            try:
                self.i_del(i)
            except KeyError:
                pass


    async def getattr(self, inode, ctx, _res=None):
        '''Get attributes for *inode*

        *ctx* will be a `RequestContext` instance.

        This method should return an `EntryAttributes` instance with the
        attributes of *inode*. The `~EntryAttributes.entry_timeout` attribute is
        ignored in this context.
        '''

        p = self.i_path(inode)
        if _res is None:
            try:
                d = await self._link.send("fg",str(p))
            except ServerError as err:
                self.raise_error(err, inode)
        else:
            d = _res

        r = EntryAttributes()
        t = d['t']
        r.st_ino = inode
        r.entry_timeout = 300
        r.attr_timeout = 300
        if d.get('m') == 'd':
            r.st_mode = stat.S_IFDIR| 0o777
        elif d.get('m') == 'f':
            r.st_mode = stat.S_IFREG| 0o666
        r.st_mtime_ns = t*1_000_000_000
        r.st_ctime_ns = t*1_000_000_000
        r.st_atime_ns = t*1_000_000_000
        r.st_birthtime_ns = t*1_000_000_000
        r.st_size = d.get('s',0)
        r.st_blksize = 256
        r.st_blocks = (r.st_size - 1) // r.st_blksize + 1
        return r


    async def setattr(self, inode, attr, fields, fh, ctx):
        '''Change attributes of *inode*

        *fields* will be an `SetattrFields` instance that specifies which
        attributes are to be updated. *attr* will be an `EntryAttributes`
        instance for *inode* that contains the new values for changed
        attributes, and undefined values for all other attributes.

        Most file systems will additionally set the
        `~EntryAttributes.st_ctime_ns` attribute to the current time (to
        indicate that the inode metadata was changed).

        If the syscall that is being processed received a file descriptor
        argument (like e.g. :manpage:`ftruncate(2)` or :manpage:`fchmod(2)`),
        *fh* will be the file handle returned by the corresponding call to the
        `open` handler. If the syscall was path based (like
        e.g. :manpage:`truncate(2)` or :manpage:`chmod(2)`), *fh* will be
        `None`.

        *ctx* will be a `RequestContext` instance.

        The method should return an `EntryAttributes` instance (containing both
        the changed and unchanged values).
        '''

        logger.warning("NotImpl: setattr: i=%r a=%r f=%r h=%r ctx=%r", inode, attr, fields, fh, ctx)
        raise FUSEError(errno.ENOSYS)

    async def readlink(self, inode, ctx):
        '''Return target of symbolic link *inode*.

        *ctx* will be a `RequestContext` instance.
        '''

        logger.warning("NotImpl: readlink: i=%r ctx=%r", inode, ctx)
        raise FUSEError(errno.ENOSYS)


    async def mknod(self, parent_inode, name, mode, rdev, ctx):
        '''Create (possibly special) file

        This method must create a (special or regular) file *name* in the
        directory with inode *parent_inode*. Whether the file is special or
        regular is determined by its *mode*. If the file is neither a block nor
        character device, *rdev* can be ignored. *ctx* will be a
        `RequestContext` instance.

        The method must return an `EntryAttributes` instance with the attributes
        of the newly created directory entry.

        (Successful) execution of this handler increases the lookup count for
        the returned inode by one.
       '''

        logger.warning("NotImpl: mknod: p=%r n=%r m=%r d=%r ctx=%r", parent_inode, name, mode, rdev, ctx)
        raise FUSEError(errno.ENOSYS)

    async def mkdir(self, parent_inode, name, mode, ctx):
        '''Create a directory

        This method must create a new directory *name* with mode *mode* in the
        directory with inode *parent_inode*. *ctx* will be a `RequestContext`
        instance.

        This method must return an `EntryAttributes` instance with the
        attributes of the newly created directory entry.

        (Successful) execution of this handler increases the lookup count for
        the returned inode by one.
        '''

        logger.warning("NotImpl: mkdir: p=%r n=%r m=%r ctx=%r", parent_inode, name, mode, ctx)
        raise FUSEError(errno.ENOSYS)

    async def unlink(self, parent_inode, name, ctx):
        '''Remove a (possibly special) file

        This method must remove the (special or regular) file *name* from the
        direcory with inode *parent_inode*.  *ctx* will be a `RequestContext`
        instance.

        If the inode associated with *file* (i.e., not the *parent_inode*) has a
        non-zero lookup count, or if there are still other directory entries
        referring to this inode (due to hardlinks), the file system must remove
        only the directory entry (so that future calls to `readdir` for
        *parent_inode* will no longer include *name*, but e.g. calls to
        `getattr` for *file*'s inode still succeed). (Potential) removal of the
        associated inode with the file contents and metadata must be deferred to
        the `forget` method to be carried out when the lookup count reaches zero
        (and of course only if at that point there are no more directory entries
        associated with the inode either).

        '''

        p = self.i_path(parent_inode) / name.decode()
        try:
            await self._link.send("fu",str(p))
        except ServerError as err:
            self.raise_error(err)

    async def rmdir(self, parent_inode, name, ctx):
        '''Remove directory *name*

        This method must remove the directory *name* from the direcory with
        inode *parent_inode*. *ctx* will be a `RequestContext` instance. If
        there are still entries in the directory, the method should raise
        ``FUSEError(errno.ENOTEMPTY)``.

        If the inode associated with *name* (i.e., not the *parent_inode*) has a
        non-zero lookup count, the file system must remove only the directory
        entry (so that future calls to `readdir` for *parent_inode* will no
        longer include *name*, but e.g. calls to `getattr` for *file*'s inode
        still succeed). Removal of the associated inode holding the directory
        contents and metadata must be deferred to the `forget` method to be
        carried out when the lookup count reaches zero.

        (Since hard links to directories are not allowed by POSIX, this method
        is not required to check if there are still other directory entries
        refering to the same inode. This conveniently avoids the ambigiouties
        associated with the ``.`` and ``..`` entries).
        '''

        p = self.i_path(parent_inode) / name.decode()
        try:
            await self._link.send("fU",str(p))
        except ServerError as err:
            self.raise_error(err)

    async def symlink(self, parent_inode, name, target, ctx):
        '''Create a symbolic link

        This method must create a symbolink link named *name* in the directory
        with inode *parent_inode*, pointing to *target*.  *ctx* will be a
        `RequestContext` instance.

        The method must return an `EntryAttributes` instance with the attributes
        of the newly created directory entry.

        (Successful) execution of this handler increases the lookup count for
        the returned inode by one.
        '''

        logger.warning("NotImpl: symlink: p=%r n=%r t=%r ctx=%r", parent_inode, name, target, ctx)
        raise FUSEError(errno.ENOSYS)

    async def rename(self, parent_inode_old, name_old, parent_inode_new,
               name_new, flags, ctx):
        '''Rename a directory entry.

        This method must rename *name_old* in the directory with inode
        *parent_inode_old* to *name_new* in the directory with inode
        *parent_inode_new*.  If *name_new* already exists, it should be
        overwritten.

        *flags* may be `RENAME_EXCHANGE` or `RENAME_NOREPLACE`. If
        `RENAME_NOREPLACE` is specified, the filesystem must not overwrite
        *name_new* if it exists and return an error instead. If
        `RENAME_EXCHANGE` is specified, the filesystem must atomically exchange
        the two files, i.e. both must exist and neither may be deleted.

        *ctx* will be a `RequestContext` instance.

        Let the inode associated with *name_old* in *parent_inode_old* be
        *inode_moved*, and the inode associated with *name_new* in
        *parent_inode_new* (if it exists) be called *inode_deref*.

        If *inode_deref* exists and has a non-zero lookup count, or if there are
        other directory entries referring to *inode_deref*), the file system
        must update only the directory entry for *name_new* to point to
        *inode_moved* instead of *inode_deref*.  (Potential) removal of
        *inode_deref* (containing the previous contents of *name_new*) must be
        deferred to the `forget` method to be carried out when the lookup count
        reaches zero (and of course only if at that point there are no more
        directory entries associated with *inode_deref* either).
        '''

        logger.warning("NotImpl: rename: p=%r n=%r d=%r n=%r f=%r ctx=%r", parent_inode_old, name_old, parent_inode_new, name_new, flags, ctx)
        raise FUSEError(errno.ENOSYS)

    async def link(self, inode, new_parent_inode, new_name, ctx):
        '''Create directory entry *name* in *parent_inode* refering to *inode*.

        *ctx* will be a `RequestContext` instance.

        The method must return an `EntryAttributes` instance with the
        attributes of the newly created directory entry.

        (Successful) execution of this handler increases the lookup count for
        the returned inode by one.
        '''

        logger.warning("NotImpl: link: i=%r p=%r n=%r ctx=%r", inode, new_parent_inode, new_name, ctx)
        raise FUSEError(errno.ENOSYS)

    async def open(self, inode, flags, ctx):
        '''Open a inode *inode* with *flags*.

        *ctx* will be a `RequestContext` instance.

        *flags* will be a bitwise or of the open flags described in the
        :manpage:`open(2)` manpage and defined in the `os` module (with the
        exception of ``O_CREAT``, ``O_EXCL``, ``O_NOCTTY`` and ``O_TRUNC``)

        This method must return a `FileInfo` instance. The `FileInfo.fh` field
        must contain an integer file handle, which will be passed to the `read`,
        `write`, `flush`, `fsync` and `release` methods to identify the open
        file. The `FileInfo` instance may also have relevant configuration
        attributes set; see the `FileInfo` documentation for more information.
        '''
        fh = FileInfo()
        if flags & os.O_RDWR:
            m = "a+" if flags & os.O_APPEND else "r+"
        elif flags & os.O_WRONLY:
            m = "a" if flags & os.O_APPEND else "w"
        else:
            m = "r"

        fd = await self._link.send("fo",str(self.i_path(inode)), fm=m)
        self.f_open(fd,inode)
        fh.fh = fd
        return fh

    async def read(self, fh, off, size):
        '''Read *size* bytes from *fh* at position *off*

        *fh* will by an integer filehandle returned by a prior `open` or
        `create` call.

        This function should return exactly the number of bytes requested except
        on EOF or error, otherwise the rest of the data will be substituted with
        zeroes.
        '''

        if size <= self.max_read:
            return await self._link.send("fr",fh,fo=off,fs=size)

        # OWCH. Need to break that large read up.

        data = []
        while size > 0:
            dl = min(size,self.max_read)
            buf = await self._link.send("fr",fh,fo=off,fs=dl)
            if not len(buf):
                break
            data.append(buf)
            if len(buf) < dl:
                break
            size -= dl
            off += dl
        return b''.join(data)

    async def write(self, fh, off, buf):
        '''Write *buf* into *fh* at *off*

        *fh* will by an integer filehandle returned by a prior `open` or
        `create` call.

        This method must return the number of bytes written. However, unless the
        file system has been mounted with the ``direct_io`` option, the file
        system *must* always write *all* the provided data (i.e., return
        ``len(buf)``).
        '''

        if len(buf) <= self.max_read:
            return await self._link.send("fw",fh,fo=off,fd=buf)
        
        # OWCH. Break that up.
        sent = 0
        while sent < len(buf):
            sn = await self._link.send("fw",fh,fo=off+sent,fd=buf[sent:sent+self.max_read])
            sent += sn
            if sn < self.max_read:
                break
        return sent

    async def flush(self, fh):
        '''Handle close() syscall.

        *fh* will by an integer filehandle returned by a prior `open` or
        `create` call.

        This method is called whenever a file descriptor is closed. It may be
        called multiple times for the same open file (e.g. if the file handle
        has been duplicated).
        '''

        pass

    async def release(self, fh):
        '''Release open file

        This method will be called when the last file descriptor of *fh* has
        been closed, i.e. when the file is no longer opened by any client
        process.

        *fh* will by an integer filehandle returned by a prior `open` or
        `create` call. Once `release` has been called, no future requests for
        *fh* will be received (until the value is re-used in the return value of
        another `open` or `create` call).

        This method may return an error by raising `FUSEError`, but the error
        will be discarded because there is no corresponding client request.
        '''
        self.f_close(fh)
        await self._link.send("fc",fh)

    async def fsync(self, fh, datasync):
        '''Flush buffers for open file *fh*

        If *datasync* is true, only the file contents should be
        flushed (in contrast to the metadata about the file).

        *fh* will by an integer filehandle returned by a prior `open` or
        `create` call.
        '''
        import pdb;pdb.set_trace()

        pass

    async def opendir(self, inode, ctx):
        '''Open the directory with inode *inode*

        *ctx* will be a `RequestContext` instance.

        This method should return an integer file handle. The file handle will
        be passed to the `readdir`, `fsyncdir` and `releasedir` methods to
        identify the directory.
        '''

        p = self.i_path(inode)
        try:
            dc = await self._link.send("fd",str(p))
        except ServerError as err:
            self.raise_error(err)

        self._last_dir_fh += 1
        fh = self._last_dir_fh
        self._dir_content[fh] = (inode,dc,ctx)
        return fh


    async def readdir(self, fh, start_id, token):
        '''Read entries in open directory *fh*.

        This method should list the contents of directory *fh* (as returned by a
        prior `opendir` call), starting at the entry identified by *start_id*.

        Instead of returning the directory entries directly, the method must
        call `readdir_reply` for each directory entry. If `readdir_reply`
        returns True, the file system must increase the lookup count for the
        provided directory entry by one and call `readdir_reply` again for the
        next entry (if any). If `readdir_reply` returns False, the lookup count
        must *not* be increased and the method should return without further
        calls to `readdir_reply`.

        The *start_id* parameter will be either zero (in which case listing
        should begin with the first entry) or it will correspond to a value that
        was previously passed by the file system to the `readdir_reply`
        function in the *next_id* parameter.

        If entries are added or removed during a `readdir` cycle, they may or
        may not be returned. However, they must not cause other entries to be
        skipped or returned more than once.

        :file:`.` and :file:`..` entries may be included but are not
        required. However, if they are reported the filesystem *must not*
        increase the lookup count for the corresponding inodes (even if
        `readdir_reply` returns True).
        '''

        dir_inode,dc,ctx = self._dir_content[fh]
        p = self.i_path(dir_inode)

        for name in dc[start_id:]:
            inode = self.i_add(p / name)
            try:
                attr = await self.getattr(inode, ctx)
            except ServerError as err:
                if err.s == "nf":
                    self.i_del(inode)
                    continue
                raise

            start_id += 1
            if not pyfuse3.readdir_reply(token,name.encode("utf-8"),attr,start_id):
                break

    async def releasedir(self, fh):
        '''Release open directory

        This method will be called exactly once for each `opendir` call. After
        *fh* has been released, no further `readdir` requests will be received
        for it (until it is opened again with `opendir`).
        '''

        del self._dir_content[fh]

    async def fsyncdir(self, fh, datasync):
        '''Flush buffers for open directory *fh*

        If *datasync* is true, only the directory contents should be
        flushed (in contrast to metadata about the directory itself).
        '''

        pass

    async def statfs(self, ctx):
        '''Get file system statistics

        *ctx* will be a `RequestContext` instance.

        The method must return an appropriately filled `StatvfsData` instance.
        '''

        logger.warning("NotImpl: statfs: ctx=%r", ctx)
        raise FUSEError(errno.ENOSYS)

    def stacktrace(self):
        '''Asynchronous debugging

        This method will be called when the ``fuse_stacktrace`` extended
        attribute is set on the mountpoint. The default implementation logs the
        current stack trace of every running Python thread. This can be quite
        useful to debug file system deadlocks.
        '''

        import sys
        import traceback

        code = list()
        for threadId, frame in sys._current_frames().items():
            code.append("\n# ThreadID: %s" % threadId)
            for filename, lineno, name, line in traceback.extract_stack(frame):
                code.append('%s:%d, in %s' % (os.path.basename(filename), lineno, name))
                if line:
                    code.append("    %s" % (line.strip()))

        logger.error("\n".join(code))

    async def setxattr(self, inode, name, value, ctx):
        '''Set extended attribute *name* of *inode* to *value*.

        *ctx* will be a `RequestContext` instance.

        The attribute may or may not exist already. Both *name* and *value* will
        be of type `bytes`. *name* is guaranteed not to contain zero-bytes
        (``\\0``).
        '''

        logger.warning("NotImpl: setxattr")
        raise FUSEError(errno.ENOSYS)

    async def getxattr(self, inode, name, ctx):
        '''Return extended attribute *name* of *inode*

        *ctx* will be a `RequestContext` instance.

        If the attribute does not exist, the method must raise `FUSEError` with
        an error code of `ENOATTR`. *name* will be of type `bytes`, but is
        guaranteed not to contain zero-bytes (``\\0``).
        '''

        logger.warning("NotImpl: getxattr")
        raise FUSEError(errno.ENOSYS)

    async def listxattr(self, inode, ctx):
        '''Get list of extended attributes for *inode*

        *ctx* will be a `RequestContext` instance.

        This method must return a sequence of `bytes` objects.  The objects must
        not include zero-bytes (``\\0``).
        '''

        logger.warning("NotImpl: listxattr")
        raise FUSEError(errno.ENOSYS)

    async def removexattr(self, inode, name, ctx):
        '''Remove extended attribute *name* of *inode*

        *ctx* will be a `RequestContext` instance.

        If the attribute does not exist, the method must raise `FUSEError` with
        an error code of `ENOATTR`. *name* will be of type `bytes`, but is
        guaranteed not to contain zero-bytes (``\\0``).
        '''

        logger.warning("NotImpl: removexattr")
        raise FUSEError(errno.ENOSYS)


    async def access(self, inode, mode, ctx):
        '''Check if requesting process has *mode* rights on *inode*.

        *ctx* will be a `RequestContext` instance.

        The method must return a boolean value.

        If the ``default_permissions`` mount option is given, this method is not
        called.

        When implementing this method, the `get_sup_groups` function may be
        useful.
        '''

        logger.warning("NotImpl: access: i=%r m=%r ctx=%r", inode, mode, ctx)
        raise FUSEError(errno.ENOSYS)

    async def create(self, parent_inode, name, mode, flags, ctx):
        '''Create a file with permissions *mode* and open it with *flags*

        *ctx* will be a `RequestContext` instance.

        The method must return a tuple of the form *(fi, attr)*, where *fi* is a
        FileInfo instance handle like the one returned by `open` and *attr* is
        an `EntryAttributes` instance with the attributes of the newly created
        directory entry.

        (Successful) execution of this handler increases the lookup count for
        the returned inode by one.
        '''

        p = self.i_path(parent_inode) / name.decode()
        try:
            i = self.i_path(p)
            gen = False
        except FUSEError:
            i = self.i_add(p)
            gen = True

        try:
            r = await self._link.send("fn",str(p))
        except ServerError as err:
            if gen:
                self.i_del(i)
            self.raise_error(err)
        else:
            i = self.i_add(p)
            h = await self.open(i,flags,ctx)
            a = await self.getattr(i,ctx,_res=r)
            return (h,a)
