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
from microfuse.util import attrdict
from microfuse.link import Link

click.anyio_backend="trio"


@click.group()
@click.option("-v","--verbose","verbose", is_flag=True,help="tell what's happening")
@click.argument("socket", type=click.Path())
@click.pass_context
async def main(ctx, socket, verbose):
    ctx.obj = attrdict()
    ctx.obj.link = await ctx.with_async_resource(Link(socket))


@main.command()
@click.option("-t","--timeout", type=int, help="Timeout to use")
@click.argument("message", type=str, nargs=-1)
@click.pass_obj
async def ping(obj, timeout, message):
    if message:
        message = " ".join(message)
    else:
        message = None
    await obj.link.ping(timeout=timeout, msg=message)


@main.command()
@click.argument("path", type=click.Path(file_okay=False, dir_okay=True), nargs=1)
@click.pass_obj
async def mount(obj, path):
    await obj.link.mount(path)


if __name__ == "__main__":
    main()
