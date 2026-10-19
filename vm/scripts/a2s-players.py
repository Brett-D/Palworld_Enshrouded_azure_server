#!/usr/bin/env python3
"""Steam A2S query helper.

  a2s-players [host] [port]          prints the player count, or exits 1 if the server doesn't answer
  a2s-players [host] [port] --list   prints a JSON list of online player names
"""
import json
import socket
import struct
import sys

args = [a for a in sys.argv[1:] if not a.startswith("--")]
host = args[0] if len(args) > 0 else "127.0.0.1"
port = int(args[1]) if len(args) > 1 else 15637
want_list = "--list" in sys.argv

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(3)


def query(request, expect):
    sock.sendto(request, (host, port))
    data, _ = sock.recvfrom(65535)
    # Servers reply with a challenge that must be appended to (or replace the -1 in) the request.
    if data[4:5] == b"A":
        challenge = data[5:9]
        if request[4:5] == b"U":
            request = request[:5] + challenge
        else:
            request = request + challenge
        sock.sendto(request, (host, port))
        data, _ = sock.recvfrom(65535)
    if data[4:5] != expect:
        raise ValueError("unexpected reply")
    return data


def read_cstr(data, pos):
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("utf-8", "replace"), end + 1


try:
    if want_list:
        data = query(b"\xff\xff\xff\xffU\xff\xff\xff\xff", b"D")
        count, pos, names = data[5], 6, []
        for _ in range(count):
            pos += 1  # index
            name, pos = read_cstr(data, pos)
            pos += 8  # score (long) + duration (float)
            if name:
                names.append(name[:64])
        print(json.dumps(names))
    else:
        data = query(b"\xff\xff\xff\xffTSource Engine Query\x00", b"I")
        pos = 6
        for _ in range(4):  # name, map, folder, game
            pos = data.index(b"\x00", pos) + 1
        pos += 2  # app id
        print(data[pos])
except (OSError, ValueError, IndexError, struct.error):
    sys.exit(1)
