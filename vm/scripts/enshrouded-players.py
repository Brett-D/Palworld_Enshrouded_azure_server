#!/usr/bin/env python3
"""Reads an Enshrouded server log on stdin and prints the players currently connected as JSON: [{"name": ...}].

Enshrouded's Steam query reports how many players are online but not their names, so names come from the
log's join, login and leave lines. The optional argument is the player count from the Steam query; it is
treated as authoritative, so players the log could not name still appear.
"""
import json
import re
import sys

ADDED = re.compile(r"\[online\] Added peer (\S+) \(steamid:(\d+)\)")
REMOVED = re.compile(r"\[online\] Removed peer (\S+)")
LOGIN = re.compile(r"\[server\] Player '(.{1,64}?)' logged in")

peers = {}  # peer id -> name; dicts keep join order
for line in sys.stdin:
    m = ADDED.search(line)
    if m:
        peers[m.group(1)] = None
        continue
    m = REMOVED.search(line)
    if m:
        peers.pop(m.group(1), None)
        continue
    m = LOGIN.search(line)
    if m:
        # The login line has no peer id; it belongs to the most recent player who joined without a name yet.
        for peer in reversed(list(peers)):
            if peers[peer] is None:
                peers[peer] = m.group(1)
                break

names = [n if n else "Player (name not reported)" for n in peers.values()]
try:
    count = int(sys.argv[1])
except (IndexError, ValueError):
    count = None
if count is not None:
    if len(names) > count:
        # A missed leave line would leave an old player behind; the oldest entries go first.
        names = names[len(names) - count:] if count else []
    else:
        names += ["Player (name not reported)"] * (count - len(names))
print(json.dumps([{"name": n} for n in names]))
