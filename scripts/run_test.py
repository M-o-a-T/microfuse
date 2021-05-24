#!/usr/bin/python3

import anyio
from anyio.streams.text import TextReceiveStream
from tempfile import TemporaryDirectory
import os
from pathlib import Path
import subprocess
import sys
import asyncclick as click
from contextlib import contextmanager

from microfuse.multiplex import Multiplexer
from microfuse.link import Link
import logging
logger = logging.getLogger(__name__)

click.anyio_backend="trio"

class ExecError(Exception):
    def __init__(self,msg, args):
        self.args=args
        self.msg=msg

    def __repr__(self):
        return '''ExecError(%r,"""\\
%s
""")''' % (self.args,self.msg)
    __str__ = __repr__


async def grab_stderr(proc, args):
    res = ""
    async for text in TextReceiveStream(proc.stderr):
        res += text
    if res:
        raise ExecError(res, args)

async def build_lib(dst,src, libs,no_mpy,verbose):
    args = ["../smurf-upy/cp.sh","-a","x64","-mcache-lookup-bc","-l","./embedded"]
    for l in libs:
        args.extend(["-l",l])
    if no_mpy:
        args.append("-n")
    args.extend([src,dst])
    if verbose:
        print("Starting:",args)
    await anyio.run_process(args, input=b"", stdout=sys.stdout, stderr=sys.stderr)

async def uclient(lib,verbose, *, task_status):
    args = ['env','MICROPYPATH=%s'%(lib,), 'micropython','test/client_unix.py']
    if verbose:
        print("Starting:",args)
    async with await anyio.open_process(args) as process:
        res = ""
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(grab_stderr, process, args)
                async for text in TextReceiveStream(process.stdout):
                    print(text, end="")
                    if text.startswith("*Ready") or "\n*Ready" in text:
                        if verbose:
                            print("micropython ready")
                        task_status.started()
                    res += text
        finally:
            process.kill()
    return res

@contextmanager
def tempdir(persist):
    if persist:
        persist = Path(persist)
        if not persist.is_dir():
            persist.mkdir()
        yield persist
    else:
        with TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

async def mplex(tmp, *, task_status):
    host="127.0.0.1"
    # port=40000+(os.getpid()%10000)
    port=8267
    console=tmp/"console"
    stream=tmp/"link"
    mplex = Multiplexer(host,port, console, stream)
    await mplex.run(task_status=task_status)

async def check_mount(tmp):
    mpt = tmp / "mpt"
    mdata = tmp / "data"

    async with Link(tmp/"link") as link:
        mdata.mkdir()
        mpt.mkdir()

        await link.send("f_c",d=str(mdata))
        async with link.mount(mpt):
            try:
                # await anyio.to_thread.run_sync(fstest)
                await anyio.run_process([sys.argv[0],"fstest",mdata,mpt],stdout=sys.stdout,stderr=sys.stderr)
            finally:
                logger.debug("Done with file system %r",mpt)

@click.group(invoke_without_command=True)
@click.option("-n","--no-mpy","no_mpy", is_flag=True,help="use uncompressed files")
@click.option("-v","--verbose","verbose", is_flag=True,help="tell what's happening")
@click.option("-d","--dir", "dir_", type=click.Path(file_okay=False, dir_okay=True), help="(persistent) state directory")
@click.option("-l","--lib","libs", multiple=True,type=click.Path(exists=False, file_okay=False, dir_okay=True), help="Additional library")
@click.pass_context
async def main(ctx,libs,no_mpy,dir_,verbose):
    logging.basicConfig(level=logging.DEBUG)
    if ctx.invoked_subcommand is not None:
        return
    with tempdir(dir_) as tmpdir:
        tmp = Path(tmpdir)
        lib = tmp / "lib"
        if not lib.is_dir():
            lib.mkdir()
        await build_lib(lib,"test", libs,no_mpy,verbose)

        async with anyio.create_task_group() as tg:
            await tg.start(uclient,lib,verbose)
            await tg.start(mplex,tmp)
            # The connection is up.
            async with anyio.create_task_group() as tg2:
                tg2.start_soon(check_mount, tmp)

            # DONE with tests!
            tg.cancel_scope.cancel()

@main.command()
@click.argument("subdir", type=click.Path(file_okay=False, dir_okay=True))
@click.argument("mountpoint", type=click.Path(file_okay=False, dir_okay=True))
def fstest(subdir,mountpoint):
    # run in a different thread, exercise the thing
    ## due to Python bug https://bugs.python.org/issue44219 this may
    ## not run in a separate thread, you need a separate process.
    def test_d(d,*f):
        fl = [fn.name for fn in d.iterdir()]
        if sorted(fl) != sorted(f):
            raise RuntimeError("DIR %r / want %r" % (fl,f))

    subdir = Path(subdir)
    mountpoint = Path(mountpoint)
    with (subdir / "test1").open("w") as f:
        f.write("Test1\n123\n")
    test_d(subdir, "test1")

    with (mountpoint / "test2").open("w") as f:
        f.write("Test2\n234\n")
    test_d(subdir, "test1","test2")
    test_d(mountpoint, "test1","test2")

    with (mountpoint / "test1").open("r") as f:
        assert f.read() == "Test1\n123\n"
    with (subdir / "test2").open("r") as f:
        assert f.read() == "Test2\n234\n"

    (mountpoint / "test2").unlink()
    test_d(mountpoint, "test1")
    test_d(subdir, "test1")

try:
    main()
except ExecError as e:
    print(e.args, file=sys.stderr)
    print(e.msg, file=sys.stderr)
    
    sys.exit(1)
