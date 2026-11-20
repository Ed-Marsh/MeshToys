# Running MeshToys on a Raspberry Pi Zero W

The bridge can run on a Raspberry Pi Zero W instead of a PC, with the Heltec plugged into the Pi by USB. It is pure Python, so nothing needs compiling. With a USB link, the bridge keeps working when the router or internet is down, apart from the weather commands, which need internet.

**What you need**

- Raspberry Pi Zero W, a microSD card and a 5 V micro-USB power supply
- A Heltec V4 (or another Meshtastic node that shows up as a USB serial port)
- A micro-USB OTG cable or adapter that ends in a USB-C plug for the Heltec

**1. Flash the card.** In [Raspberry Pi Imager](https://www.raspberrypi.com/software/) choose Raspberry Pi OS Lite (no desktop), then use the customisation options to set a hostname, username and password, your WiFi, and to enable SSH. The Zero W has 2.4 GHz WiFi only (802.11 b/g/n), so it will not see a 5 GHz-only network. WiFi is only needed for you to log in over SSH. The bridge itself does not use it when connected by USB.

**2. Wire it.** The Zero W has two micro-USB ports. Go by the labels printed on the board:

| Port | Use |
|---|---|
| `PWR IN` (the one nearest the corner) | Power supply only |
| `USB` (the middle one) | Data. Plug the OTG cable here, with the other end in the Heltec's USB-C port |

The Heltec's USB-C port carries both its power and its serial data, so one cable does both. Do not plug the Heltec into `PWR IN`: that port has no data connection. The Pi's `PWR IN` supply also ends up powering the Heltec. How much current a Zero W's USB port can safely supply to a transmitting Heltec V4 has not been measured here. If the node resets or the bridge drops out when it transmits, suspect power first and try a better supply.

**3. Install.** Log in with `ssh youruser@yourhostname.local` (or use the Pi's IP address), then:

```
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3-venv python3-pip
git clone https://github.com/Ed-Marsh/MeshToys.git
cd MeshToys
python3 -m venv .venv
source .venv/bin/activate
pip install meshtastic pypubsub requests
```

Each time you log in again, run `source .venv/bin/activate` from the `MeshToys` folder before running the script by hand. Your prompt starts with `(.venv)` when it is active. If you get `ModuleNotFoundError: No module named 'meshtastic'`, the environment is not active.

**4. Find the port and test.**

```
ls /dev/ttyACM* /dev/ttyUSB*
python mesh_bridge.py --lat 51.50 --lon -0.12 --port /dev/ttyACM0 --dm-only --log-file mesh.log -v
```

The V4 appeared as `/dev/ttyACM0` on the Pi this was written on. Use your own latitude and longitude (the numbers above are an example). If you get a permission error on the port, add your user to the `dialout` group with `sudo usermod -aG dialout $USER`, then log out and in again. `--host` overrides `--port`, so leave `--host` out when using USB. Only one program can hold the serial port, so stop any other copy of the bridge (for example on a PC) and close any Meshtastic app that has the node open.

**5. Start it at boot.** Create `/etc/systemd/system/meshtoys.service` with `sudo nano`, replacing `youruser`, the folder, and the coordinates, and adjusting the other options to suit:

```
[Unit]
Description=MeshToys bridge
After=multi-user.target

[Service]
User=youruser
WorkingDirectory=/home/youruser/MeshToys
ExecStart=/home/youruser/MeshToys/.venv/bin/python mesh_bridge.py --lat 51.50 --lon -0.12 --port /dev/ttyACM0 --dm-only --admin '!b3725444' --announce "{short}: DM me /help" --log-file mesh.log
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then enable and start it:

```
sudo systemctl daemon-reload
sudo systemctl enable --now meshtoys
systemctl status meshtoys
```

`status` should show `enabled` and `active (running)`. If it shows `disabled` or `inactive (dead)`, the `enable --now` step did not run. To watch the log, use `journalctl -u meshtoys -f`. The service calls the virtual environment's Python directly, so it does not need `source .venv/bin/activate`. `Restart=always` brings it back if the script crashes. If the Heltec ever shows up under a different name than `/dev/ttyACM0` (for example `ttyACM1` after being replugged), the service will not connect until you change `--port`.

**Updating.** Stop the service or leave it running, then:

```
cd ~/MeshToys
git pull
sudo systemctl restart meshtoys
```

If `git pull` complains about local changes, check `git status` first. If a new version adds a dependency, install it with the environment active.

**Status.** Checked on a Pi Zero W with a Heltec V4 over USB: the bridge connects and answers `/wumpus`, and the systemd service starts and connects, including automatically after a reboot. Not yet checked on the Pi: `/trek`, `/zork` and `/adventure`, long-term running, and the power draw with a battery or UPS.
