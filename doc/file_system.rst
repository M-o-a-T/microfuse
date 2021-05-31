File system
===========

MicroFuse's main purpose (one of them, anyway) is to allow mounting the
embedded system's file system.

Here we document the messages used for this feature.

Messages
========

f_c
-----

Open / Connect.

This message causes all open files to be closed.

* d

  Directory. All subsequent file accesses are relative to this directory.

fc
-----

Close a file.

* d

  Open file descriptor number.

fd
-----

Directory content. Returned as a list of entries.

* d

  Path to look up, must be a directory.

fD
-----

Create a new directory.

* d

  Pathname of the directory. Must not exist.

fg
-----

Getattr. Returns a file's state.

* d

  Path to the file

The return value is a dict with these keys:

* m

  File type. 'f' or 'd' for files or directories, respectively.

* s

  Size of the file, in bytes. Not sent for directories.

* t

  Modification timestamp.

fm
-----

Move a file.

* s

  Source path

* d

  Destination path

* x

  Exchange file. If source and destination shall be exchanged, this is a
  third path, which must not exist, that's used as a temporary name.
  In this case obviously the destination must exist.

* n

  Flag. If set, the destination must not exist. Otherwise it is sliently
  replaced (assuming ``x`` is not present).

fn
-----

  Create a new empty file.

* d

  Path of the new file.

fo
-----

Open a file, always in binary mode.

* d

  Path of the file

* fm

  Opening mode. As in Python's ``open`` call.

This call returns a file descriptor number, for use in successive ``read``,
``write`` and ``close`` calls.

fr
-----

Read file contents.

* d

  File descriptor number

* fo

  Offset to read at

* fs

  Number of bytes to read

This call returns the bytes read.

fw
-----

Write file contents.

* d

  File descriptor number

* fo

  Offset to write at

* fd

  Bytes to write

This call returns the number of bytes written.

fu
-----

Unlink / remove a file.

* d

  Path of the file to unlink.

fU
-----

Remove a directory. The directory must be empty.

* d

  Path of the directory to remove.


Errors
======

All errors translate to ``EIO``, with the exception of

* fn

  File not found, ``ENOENT``

* fx

  File exists, ``EEXIST``
