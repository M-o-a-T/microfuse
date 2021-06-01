#!/usr/bin/micropython
import ufuse
import usys

u = ufuse.UFuse()
u.start()
print("*Ready")
u._accept(u.listen_s)
c = list(u.clients)[0]
print("Client:", c)
while c.sock is not None:
    c._read(None)
c.close()
print("OK")
usys.exit(0)
