# Troubleshooting

## A direct message gets no reply

**Symptom.** A node sends a direct message such as `/help` to the bridge's node and its app shows "Relayed, not confirmed by recipient", but no reply ever arrives. Other nodes can talk to the bridge normally, and a traceroute from the failing node to the bridge's node works.

**Check whether the bridge got it.** Send the message again, then on the machine running the bridge look for a line with the sender's node ID:

```
journalctl -u meshtoys --since "2 min ago" --no-pager | grep -E "\->|DM from"
```

Test the check first by sending a command from a node that is working, which should produce a line like `!b3725444 -> /help`. If the failing node's ID never appears, the bridge's node did not receive or decode its message, so the bridge never saw it. If you run the bridge by hand instead of as a service, look in `mesh.log` or the terminal.

**Likely cause, seen once.** Direct messages are encrypted to the recipient's public key, so the bridge's node needs the sender's current key. If the sender was reinstalled or reset after the bridge's node first heard it, it can come back with the same node ID but new keys. The Meshtastic Android app documentation says that, like the firmware, the app sticks with the first key it saw for a node and rejects a different one later. The bridge's node then keeps an old key, and may also keep the sender's old name, which is a useful clue: look at the sender's name in the bridge node's node list and see if it is out of date. This explanation fits what was seen here but was not confirmed by inspecting the keys.

**What fixed it.**

1. In the Meshtastic app, connect to the bridge's node, open the sender's entry in its node list and remove it. This clears only that node's local copy.
2. Reboot the sender so it announces itself again.
3. Wait a few minutes until the sender shows up again with its current name and a closed lock.
4. Send `/help` again.

Wait for step 3 before testing. If the sender is still missing after ten minutes or so, try **Request User Info** from the sender's app, on the bridge node's entry.
