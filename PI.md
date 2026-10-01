# PI.md — bringing up the Raspberry Pi 4 pick PC

The Pi arrives **Thursday 2026-10-01**. Part 1 is for Nick (hands on the board, ~20 min).
Part 2 is for the Claude session (Fable) that finishes the job over SSH from the Mac
Studio. The reference for everything the installer does is `deploy/pi/README.md`; the
procedure is the `deploy-pick-pc` skill. This page is the first-run plan on top of them.

**Nothing in `deploy/pi/` has run on a board yet** (written 2026-09-28, CI-checked only).
Thursday is the first run: expect to find things, and write them down (Part 2, step 6).

## The plan in one picture

| | Thursday (bench) | Later (cell) |
| --- | --- | --- |
| Pi **Wi-Fi** (`wlan0`) | office LAN `10.0.0.0/24`, DHCP: internet for the build + SSH from the Mac Studio (`10.0.0.16`) | stays: management SSH |
| Pi **Ethernet** (`eth0`) | unplugged, or already on the cell switch | cell LAN, static **`192.168.3.20/24`**, no gateway |
| D435 | on a **blue** USB 3 port | same |
| UR3e (`192.168.3.3`) | unplugged since 2026-09-29: the doctor's `robot.*` lines will fail, that is expected | on |

Two decisions baked in here, both Nick's to overrule:

- **Raspberry Pi OS Lite (64-bit), not a bare Debian image.** `deploy/pi/README.md` names
  Debian arm64 as the target and Pi OS Lite as the same thing; Imager's customisation gives
  the SSH key, the user and passwordless sudo in one pass, and an agent's shell has no TTY
  for a sudo prompt.
- **`192.168.3.20`, not `.10`.** `SETUP.md` and the README's example give the pick PC `.10`,
  which is the Mac Studio's cell address. If the Pi is to *replace* the Mac on the cell,
  say so and it takes `.10`.

---

## Part 1 — Nick, by hand

### What you need on the bench

- The Pi 4 (4 GB), its case/heatsinks, a microSD (≥ 16 GB; the kit's is 32 GB) and a card
  reader.
- A **5.1 V / 3 A USB-C supply**. The D435 draws from the Pi's USB budget (1.2 A total on a
  3 A supply); a phone charger is the first suspect if the camera drops out.
- The D435 and a **USB 3** C-to-A cable, short, no hub.
- No monitor or keyboard: it is headless from the first boot.

### 1. Flash

Raspberry Pi Imager → device **Raspberry Pi 4** → OS **Raspberry Pi OS (other) → Raspberry
Pi OS Lite (64-bit)** → the card. In the OS customisation step set:

| Setting | Value |
| --- | --- |
| Hostname | `pickpc` |
| Username / password | `nick` / anything (the password is only for a console login) |
| Wireless LAN | the office network's SSID + password, **country `US`** (Wi-Fi stays off without a country) |
| SSH | enabled, **public-key authentication only**, key = the Mac Studio's |

The key to paste is the Mac Studio's, because that is where the deploy runs from:

    cat ~/.ssh/id_ed25519.pub        # on the Mac Studio; fingerprint SHA256:Xz1K1SaS…nzzDI

If you flash from another machine, copy that line across — not that machine's own key.

### 2. Assemble and boot

1. Card in, heatsinks/case on, **D435 into a blue (USB 3) port**.
2. Ethernet: leave it out for now, or plug it into the cell switch (it gets no address
   there until Part 2 sets one; harmless).
3. Power on. First boot resizes the card and reboots once: give it ~2 minutes.

### 3. Check you can get in (from the Mac Studio)

    ssh nick@pickpc.local 'hostname -I; uname -m; sudo -n true && echo sudo-ok'

Expect an address on `10.0.0.x`, `aarch64`, and `sudo-ok`.

- **`pickpc.local` doesn't resolve:** find the address on the router's DHCP list, or
  `arp -a | grep -i -E 'dc:a6:32|e4:5f:01|d8:3a:dd|2c:cf:67|28:cd:c1'` (Raspberry Pi MAC
  prefixes) after a `ping -c1 10.0.0.255`.
- **No `sudo-ok`** (sudo wants a password): on the Pi, once —

      echo "nick ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/010-nick-nopasswd
      sudo chmod 0440 /etc/sudoers.d/010-nick-nopasswd && sudo visudo -c

- **Host key warning** on a re-flash: `ssh-keygen -R pickpc.local` (and `-R <ip>`).

### 4. Hand over

Write the Pi's Wi-Fi address here, then start the session:

    Pi Wi-Fi address: 10.0.0.___

Prompt for Fable: *"The Pi is up at nick@10.0.0.___ — follow PI.md part 2."*

That is all of Part 1. Don't `apt upgrade`, don't install anything, don't set the static
address — Part 2 does those and records what it saw.

---

## Part 2 — Fable, over SSH

You are on the Mac Studio in this checkout. Load the **`deploy-pick-pc`** skill and follow
it; the steps below are the first-run specifics it doesn't know. Use the Pi's **IP
address** throughout, never `pickpc.local`: the installer's firewall drops inbound mDNS
(`deploy/pi/nftables.conf` admits only SSH, ICMP, DHCP replies and :7621/:7622), so the
name stops resolving the moment the deploy finishes.

### 0. Before touching the Pi

- Session-start audit (`git status`, branch, worktrees, reflog). Work on a fresh branch off
  `fork/dev` (`deploy/pi-first-run`) — fixes to `deploy/pi/` are expected.
- `env -u UR_CELL uv run pytest tests/test_deploy_pi.py -q` green before you start, so a
  later failure is yours.

### 1. Preflight

Run the skill's preflight table against `nick@<ip>` and report the block. First-run
additions:

    ssh nick@<ip> '. /etc/os-release; echo $ID $VERSION_CODENAME; python3 -V; systemctl --version | head -1'
    ssh nick@<ip> 'nmcli -t -f NAME,TYPE,DEVICE con show; ip -br addr; ip route'
    ssh nick@<ip> 'vcgencmd get_throttled; vcgencmd measure_temp'
    ssh nick@<ip> 'cat /proc/device-tree/model; free -m; df -h / /var/tmp'

- Record the codename. **trixie** (systemd ≥ 254) honours the unit's restart back-off;
  **bookworm** (252) logs `Unknown key` for `RestartSteps=` / `RestartMaxDelaySec=` and
  restarts every 5 s. Either is supported; it answers the open question in `TODO.md`.
- `get_throttled` must read `0x0`. Anything else is under-voltage: stop and tell Nick
  before a long compile and before blaming the camera.
- D435 link speed (the skill's sysfs one-liner): `5000` = USB 3. `480` = wrong port or
  cable; the cockpit still runs (640×480 @ 15) but say so.

### 2. Deploy

    scripts/deploy-pi.sh nick@<ip> --cell ur3 --robot-host 192.168.3.3

- **Run it in the background** with the 2 h limit and poll; the librealsense build has
  never been timed on a Pi 4. `install.sh` sizes jobs as RAM / 1536 MiB (expect `-j2` on
  4 GB). Watch it from a second call:
  `ssh nick@<ip> 'tail -n 3 /var/tmp/perceptronics-build/cmake-build.log'`.
- A killed or dropped run is safe to repeat but **starts the build from scratch**
  (`install.sh` removes the source and build trees before cloning). If it won't fit in
  2 h, run the installer on the Pi under `systemd-run --unit=perceptronics-install …` or
  `nohup` so it survives the SSH session, rather than trying a third time (two failed
  attempts → stop and describe).
- `--robot-host 192.168.3.3` also sets the firewall's cell subnet to `192.168.3.0/24`.
  That is the intent: on Thursday the cockpit's ports are therefore **closed to the office
  LAN**, and you reach them through a tunnel (step 4).
- Note the wall-clock build time from the `[install HH:MM:SS]` lines.

### 3. The cell-side address

Give Ethernet its static cell address without stealing the default route from Wi-Fi (the
README's example sets a gateway — don't, here: the cell switch has no route out and
Ethernet outranks Wi-Fi):

    ssh nick@<ip> 'nmcli -t -f NAME,TYPE,DEVICE con show'      # find the ethernet connection's name
    ssh nick@<ip> 'sudo nmcli con mod "<name>" ipv4.method manual ipv4.addresses 192.168.3.20/24 ipv4.gateway "" ipv4.never-default yes'

You are connected over Wi-Fi, so this cannot cut your session. If there is no ethernet
connection profile yet (cable never plugged), create one:
`sudo nmcli con add type ethernet ifname eth0 con-name cell ipv4.method manual ipv4.addresses 192.168.3.20/24 ipv4.never-default yes`.
Recent Pi OS images route NetworkManager through netplan/cloud-init — **verify the setting
survives `sudo reboot`** (`ip -br addr show eth0`, `ip route` still default via `wlan0`)
before calling it done. If `.20` answers a ping from anything already, stop and ask.

### 4. Verify

    scripts/deploy-pi.sh nick@<ip> --doctor-only
    ssh nick@<ip> 'systemctl is-active perceptronics-cockpit; journalctl -u perceptronics-cockpit -b -n 40 --no-pager'
    ssh nick@<ip> 'sudo nft list table inet perceptronics; ss -ltn | grep -E ":762[12]"'
    ssh -f -N -L 7621:127.0.0.1:7621 nick@<ip> && curl -s http://127.0.0.1:7621/api/info | head -c 600

(Kill the tunnel when done: `pkill -f "7621:127.0.0.1:7621"`. If something on the Mac
already holds :7621 — a local cockpit — use another local port.)

What "good" looks like with the robot unplugged:

- `sdk` ok at `/opt/librealsense/lib/librealsense2.so`; `camera … usb 3.x`;
  `cockpit … fps ~30, seq` advancing between two reads of `/api/info`.
- `robot.*` lines **fail** (no UR3e on the wire). Report them verbatim as expected, not as
  deploy faults. They can only be closed once the robot is back and the Pi's Ethernet is on
  the cell switch.
- `handeye` reads `env:` — the `PERCEPTRONICS_T_FLANGE_CAMERA` from `ur3.env` (2026-09-27
  solve) rides in `/etc/perceptronics/cell.env`. Leave it; see step 6.

These are the README's *Open items*; each gets a yes/no from this run:

| Open item | How to answer it |
| --- | --- |
| librealsense builds on arm64 Debian outside Docker | the deploy finished; note the minutes |
| `nft -c` accepts the rendered firewall | `install.sh` runs it; `nft list table` above shows the table |
| the unit is valid | `ssh nick@<ip> 'systemd-analyze verify /etc/systemd/system/perceptronics-cockpit.service'` |
| the **hardened, non-root** unit opens the D435 | frames advancing in `/api/info`. If it fails with an access error while `sudo perceptronics-doctor --stream` (service stopped) works, look at `DeviceAllow=` / `DevicePolicy=` first |
| hardening score | `systemd-analyze security perceptronics-cockpit` (input for the unset `SystemCallFilter=`) |
| the D435 holds on the Pi 4's USB budget | 10 minutes of streaming: `seq` still advancing, `vcgencmd get_throttled` still `0x0`, no `Frame didn't arrive` in the journal |
| per-frame cost on a Pi 4 (`hardware/BOM.md`: nothing measured) | time `curl -s -o /dev/null -w '%{time_total}\n' http://127.0.0.1:7621/api/color.png` ×10 and a `GET /api/pick/scene`; note `top` for the cockpit process |

### 5. If something breaks

- Fix it **in the repo** (`deploy/pi/`, `scripts/deploy-pi.sh`), with a test in
  `tests/test_deploy_pi.py` that fails first, commit, redeploy. Never hand-edit the unit or
  the firewall on the Pi (the skill's *Don't* list).
- Build failed: `/var/tmp/perceptronics-build/cmake-{configure,build}.log`. `Killed` /
  `internal compiler error` = memory.
- Two failed approaches at the same thing → stop, write up what you tried and saw.
- The Pi is expendable: a re-flash is 10 minutes of Part 1. The UR3e's controller is not —
  nothing in this runbook sends motion, and nothing should.

### 6. Write it down, then PR

- `deploy/pi/README.md`: move each verified *Open item* out, with the date and the number
  (build minutes, OS codename, USB link, frame timings). Fix the `192.168.3.10` examples if
  Nick confirms `.20`.
- `SETUP.md` §2 (Pick PC row) and §4: the Pi's real addresses, OS, "first run 2026-10-0x".
- `hardware/BOM.md`: the Pi 4 timing line under "not verified".
- `TODO.md` → *Open questions for Nick*: bookworm vs trixie (now with the answer you saw),
  and whether the hand-eye line should stay out of the PC's `cell.env` by default.
- Memory: one entry for the Pi (address, user, what was verified).
- Draft PR into `dev` if anything under `deploy/` or `scripts/` changed; docs-only can
  auto-merge.

### Not in this session (needs the robot and Nick at the pendant)

1. UR3e powered, Pi Ethernet on the cell switch, `sudo perceptronics-doctor` green on
   `robot.*`.
2. Pendant: remove RealSense Pilot, install Perceptronic 0.6.0 from the USB stick,
   **Installation → URCaps → Perceptronic → Cockpit = `http://192.168.3.20:7621` → Save**.
3. Hand-eye on the Pi (`perceptronics calibrate --apply`), then delete the
   `PERCEPTRONICS_T_FLANGE_CAMERA` line from `/etc/perceptronics/cell.env` and restart —
   the environment value beats the saved file.
4. The pick kit's first run on the cell (`TODO.md`, 2026-09-28 entry).
