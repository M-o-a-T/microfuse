#!/usr/bin/micropython
import usys
import ufuse

u=ufuse.UFuse()
u.start()
print("*Ready")
u._accept(u.listen_s)
c=list(u.clients)[0]
print("Client:", c)
while c.sock is not None:
    c._read()
c.close()
print("OK")
usys.exit(0)
