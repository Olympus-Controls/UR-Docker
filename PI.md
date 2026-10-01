# PI.md — bringing up the Raspberry Pi 4 pick PC

The Pi arrives **Thursday 2026-10-01**. Part 1 is for Nick (hands on the board, ~20 min).
Part 2 is for the Claude session (Fable) that finishes the job over SSH from the Mac
Studio. The reference for what the installer does is `deploy/pi/README.md`; the procedure
is the `deploy-pick-pc` skill. This page is the first-run plan on top of them.

**Nothing in `deploy/pi/` has run on a board yet** (written 2026-09-28, CI-checked only).
Thursday is the first run: expect to find things, and write them down (Part 2, step 7).

## What the box is (Nick, 2026-09-30)

- **Debian**, not Raspberry Pi OS: open and non-proprietary as far as a Pi allows.
- **Offline and hardwired.** In service the only link is one Ethernet cable to the robot.
  No Wi-Fi client, no internet, ever.
- **The wired address is the commissioner's choice** — DHCP or static — set from a **web
  page reached over a hotspot the box raises during commissioning only**. No SSH, no
  keyboard, no editing files.

Three things follow, and they shape Thursday:

1. **The commissioning hotspot + setup page does not exist yet.** It is specified below
   (*The commissioning interface*) and built in Part 2, on the board, because an access
   point cannot be tested anywhere else.
2. **The first install still needs internet once** (apt, and the librealsense build
   fetches from GitHub and sqlite.org). On Thursday that is the Ethernet port on the office
   LAN (`10.0.0.0/24`, DHCP); afterwards the cable moves to the robot and stays there. A
   shipped unit gets a pre-built image instead — an open item, not Thursday's job.
3. **"Open" has a floor on a Pi.** No Pi boots without Broadcom's closed boot firmware
   (`raspi-firmware`, Debian `non-free-firmware`), and the on-board Wi-Fi the hotspot uses
   needs the `firmware-brcm80211` blob. Everything above the firmware is Debian main.

| | Thursday bench | In service |
| --- | --- | --- |
| Ethernet (`eth0`) | office LAN, DHCP, internet for the build, SSH from the Mac Studio (`10.0.0.16`) | robot / cell switch, the address the commissioner set (suggested `192.168.3.20/24`) |
| Wi-Fi (`wlan0`) | off, except while testing the hotspot | **off** (radio blocked) unless commissioning is re-armed |
| D435 | blue USB 3 port | same |
| UR3e (`192.168.3.3`) | unplugged since 2026-09-29: the doctor's `robot.*` lines fail, expected | on |

`192.168.3.20`, not the `.10` in `SETUP.md` and the README's examples: `.10` is the Mac
Studio's cell address.

---

## Part 1 — Nick, by hand

### What you need on the bench

- The Pi 4 (4 GB), case/heatsinks, a microSD (≥ 16 GB) and a card reader.
- A **5.1 V / 3 A USB-C supply**. The D435 draws from the Pi's USB budget (1.2 A total on
  a 3 A supply); a phone charger is the first suspect if the camera drops out.
- The D435 and a short **USB 3** C-to-A cable, no hub.
- An Ethernet cable to the **office** switch/router (the one the Mac Studio's `10.0.0.16`
  is on).
- No monitor or keyboard.

### 1. Flash Debian

Image: Debian's own Raspberry Pi build, **bookworm for the Pi 4** —
<https://raspi.debian.net/tested/20231109_raspi_4_bookworm.img.xz> (checksum file beside
it on <https://raspi.debian.net/tested-images/>).

Why bookworm: Debian's Pi images stopped being rebuilt in 2023–24. The bookworm one was a
stable release when built, so bringing it current is an ordinary `apt full-upgrade`
(Part 2 does it). The trixie image on the same page is a 2023 snapshot of *testing*; don't
use it. (Checked 2026-09-30.)

Either write it with Raspberry Pi Imager (**Use custom** → the `.img.xz`; its OS
customisation does not apply to a custom image, skip it), or from a terminal on the Mac:

    diskutil list                                   # find the card: /dev/diskN, check the size
    diskutil unmountDisk /dev/diskN
    xzcat 20231109_raspi_4_bookworm.img.xz | sudo dd of=/dev/rdiskN bs=4m
    
`dd` to the wrong disk erases it. Check `N` twice.

### 2. Put the SSH key on the card

Re-insert the card. macOS mounts its small FAT partition; open **`sysconf.txt`** on it and
set two lines:

    root_authorized_key=<the whole line printed by: cat ~/.ssh/id_ed25519.pub on the Mac Studio>
    hostname=pickpc

The key must be the **Mac Studio's** (fingerprint `SHA256:Xz1K1SaS…nzzDI`): the deploy runs
from there. Leave `root_pw` unset — root then has no password and can log in only with
that key over SSH (or at a physical console). Eject.

### 3. Assemble and boot

1. Card in, heatsinks/case on, **D435 into a blue (USB 3) port**.
2. **Ethernet to the office network.**
3. Power on; give it two minutes (the first boot grows the filesystem).

### 4. Check you can get in (from the Mac Studio)

The image takes a DHCP address on Ethernet. Find it on the router's lease list (`pickpc`),
or:

    ping -c1 10.0.0.255 >/dev/null; arp -a | grep -i -E 'dc:a6:32|e4:5f:01|d8:3a:dd|2c:cf:67|28:cd:c1'

then

    ssh root@10.0.0.___ 'uname -m; . /etc/os-release; echo $VERSION_CODENAME'

Expect `aarch64` and `bookworm`. A host-key warning after a re-flash:
`ssh-keygen -R 10.0.0.___`.

### 5. Hand over

    Pi address on the office LAN: 10.0.0.___

Prompt for Fable: *"The Pi is up at root@10.0.0.___ — follow PI.md part 2."*

That is all of Part 1. Don't upgrade, install or configure anything on it — Part 2 does
that and records what it saw. You come back at Part 2 step 5, with a phone.

---

## Part 2 — Fable, over SSH

You are on the Mac Studio in this checkout. Load the **`deploy-pick-pc`** skill; the steps
below are the first-run specifics it doesn't know. Use the Pi's **IP address**, never a
`.local` name.

Order matters: **get the existing deploy green first (steps 1–3), then build the
commissioning interface (step 4).** Don't start the new feature on a box whose baseline
you haven't seen work.

### 0. Before touching the Pi

- Session-start audit (`git status`, branch, worktrees, reflog). Fresh branch off
  `fork/dev`; fixes to `deploy/pi/` are expected, and step 4 is a feature branch of its own.
- `env -u UR_CELL uv run pytest tests/test_deploy_pi.py -q` green before you start.

### 1. Prepare the base system (as root)

The Debian image is minimal: root only, probably no `sudo`, and two years stale.
`scripts/deploy-pi.sh` calls `sudo -n` on the PC, so it needs an admin user first.

    ssh root@<ip> 'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade && apt-get -y install sudo'
    ssh root@<ip> 'adduser --disabled-password --gecos "" nick && adduser nick sudo \
        && install -d -m 700 -o nick -g nick /home/nick/.ssh \
        && install -m 600 -o nick -g nick /root/.ssh/authorized_keys /home/nick/.ssh/authorized_keys \
        && echo "nick ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/010-nick-nopasswd \
        && chmod 0440 /etc/sudoers.d/010-nick-nopasswd && visudo -c'
    ssh root@<ip> reboot          # onto the upgraded kernel/firmware; wait, then:
    ssh nick@<ip> 'sudo -n true && echo sudo-ok'

Check where the image put root's key before copying it (`/root/.ssh/authorized_keys` is the
expectation, not a verified fact). From here on use `nick@<ip>`.

Then look, don't assume — the image's documentation does not say what manages the network:

    ssh nick@<ip> '. /etc/os-release; echo $VERSION_CODENAME; python3 -V; systemctl --version | head -1'
    ssh nick@<ip> 'ls /etc/network/interfaces.d /etc/systemd/network 2>/dev/null; systemctl is-active networking systemd-networkd NetworkManager'
    ssh nick@<ip> 'ip -br link; ls /sys/class/net; dpkg -l | grep -E "firmware-brcm|raspi-firmware|wpasupplicant|hostapd|rfkill"'
    ssh nick@<ip> 'cat /proc/device-tree/model; free -m; df -h / /var/tmp'
    ssh nick@<ip> 'cat /sys/class/thermal/thermal_zone0/temp; dmesg | grep -i -E "voltage|thrott" | tail'

- Which of ifupdown / systemd-networkd owns `eth0` decides step 4's backend.
- `wlan0` present means the Wi-Fi blob is installed; absent means `firmware-brcm80211`
  (`non-free-firmware`) is needed for the hotspot.
- `vcgencmd` is a Raspberry Pi OS tool and won't be there; under-voltage shows in `dmesg`.
  Any under-voltage line: stop and tell Nick before the long compile.
- bookworm's systemd 252 logs `Unknown key` for the unit's `RestartSteps=` /
  `RestartMaxDelaySec=` and restarts every 5 s. Known, harmless; note it.

Then the skill's preflight table (D435 present, USB 3 link `5000`, internet, disk).

### 2. Deploy

    scripts/deploy-pi.sh nick@<ip> --cell ur3 --robot-host 192.168.3.3

- **Run it in the background** with the 2 h limit and poll; the librealsense build has
  never been timed on a Pi 4. `install.sh` sizes jobs as RAM / 1536 MiB (expect `-j2`).
  Watch: `ssh nick@<ip> 'tail -n 3 /var/tmp/perceptronics-build/cmake-build.log'`.
- A killed run is safe to repeat but **starts the build from scratch** (`install.sh`
  removes the build tree before cloning). If it won't fit in 2 h, run the installer on the
  Pi under `systemd-run` so it survives the SSH session. Two failed attempts → stop and
  describe.
- `--robot-host 192.168.3.3` makes the firewall admit :7621/:7622 from `192.168.3.0/24`
  only, so on the office LAN the cockpit is reached through a tunnel (step 3). SSH stays
  open from anywhere.
- Note the wall-clock build time from the `[install HH:MM:SS]` lines.

### 3. Verify the baseline

    scripts/deploy-pi.sh nick@<ip> --doctor-only
    ssh nick@<ip> 'systemctl is-active perceptronics-cockpit; journalctl -u perceptronics-cockpit -b -n 40 --no-pager'
    ssh nick@<ip> 'sudo nft list table inet perceptronics; ss -ltn | grep -E ":762[12]"'
    ssh -f -N -L 7621:127.0.0.1:7621 nick@<ip> && curl -s http://127.0.0.1:7621/api/info | head -c 600

(Kill the tunnel after: `pkill -f "7621:127.0.0.1:7621"`. Use another local port if a
cockpit on the Mac holds :7621.)

Good, with the robot unplugged: `sdk` ok at `/opt/librealsense/lib/librealsense2.so`;
`camera … usb 3.x`; `cockpit … fps ~30` with `seq` advancing between two reads. The
`robot.*` lines **fail** — report them verbatim as expected. `handeye` reads `env:` (the
2026-09-27 solve from `ur3.env`); leave it.

Each of the README's *Open items* gets a yes/no from this run:

| Open item | How to answer it |
| --- | --- |
| librealsense builds on arm64 Debian outside Docker | the deploy finished; note the minutes |
| `nft -c` accepts the rendered firewall | `install.sh` runs it; `nft list table` shows the table |
| the unit is valid | `systemd-analyze verify /etc/systemd/system/perceptronics-cockpit.service` |
| the **hardened, non-root** unit opens the D435 | frames advancing. If access fails while `sudo perceptronics-doctor --stream` (service stopped) works, look at `DeviceAllow=` / `DevicePolicy=` |
| hardening score | `systemd-analyze security perceptronics-cockpit` |
| the D435 holds on the Pi 4's USB budget | 10 min streaming: `seq` advancing, no under-voltage in `dmesg`, no `Frame didn't arrive` in the journal |
| per-frame cost on a Pi 4 (`hardware/BOM.md`: unmeasured) | `curl -s -o /dev/null -w '%{time_total}\n' http://127.0.0.1:7621/api/color.png` ×10; `top` for the cockpit process |

### 4. Build the commissioning interface

New work, its own branch and PR (draft: it touches `deploy/`, the firewall and a
root-privileged path). Spec in the next section. Rules for building it:

- Tests first for everything that is pure logic (address validation, rendering the network
  file, the state machine) — adversarial inputs, since this is a web form that writes
  system configuration as root.
- New packages on the PC (an access-point daemon) go through the dependency review in the
  PR description: what, why, alternatives, footprint.
- You are connected over **Ethernet**, and the feature reconfigures Ethernet. Until Nick is
  at the bench, exercise the apply path against a **dry-run / rendered file**, not the live
  interface. The first live apply is step 5, with Nick's phone on the hotspot as the way
  back in.

### 5. Commission it for real (Nick, with a phone)

1. Fable arms commissioning; Nick joins the hotspot and opens the setup page.
2. Nick sets **Static `192.168.3.20/24`, no gateway**, robot `192.168.3.3`, and applies.
   The office-LAN SSH session dies here — that is the feature working.
3. Nick moves the Ethernet cable to the cell switch. From the Mac Studio (its cell
   interface up, `192.168.3.10`): `ssh nick@192.168.3.20 'ip -br addr; sudo perceptronics-doctor'`.
4. Nick presses **Finish**; confirm the hotspot is gone (`rfkill list`, no SSID in the
   air) and stays gone across a reboot.
5. Also try the unhappy paths once: a garbage address in the form, DHCP with no server on
   the wire, power cut mid-apply.

If step 4 isn't ready when Nick wants the box on the cell, set the address by hand in
whatever step 1 found owning `eth0`, and say plainly that the hotspot is still owed.

### 6. If something breaks

- Fix it **in the repo**, regression test first, commit, redeploy. Never hand-edit the
  unit or the firewall on the Pi.
- Build failed: `/var/tmp/perceptronics-build/cmake-{configure,build}.log`. `Killed` /
  `internal compiler error` = memory.
- Locked out (bad network apply, no hotspot): the Pi is expendable — pull the card and
  re-flash, 10 minutes of Part 1. Nothing in this runbook sends robot motion, and nothing
  should.

### 7. Write it down, then PR

- `deploy/pi/README.md`: Debian image + `sysconf.txt` first boot replaces the Imager
  paragraph; each verified *Open item* moves out with its date and number; the
  commissioning section; `192.168.3.10` examples → `.20`.
- `.claude/skills/deploy-pick-pc/SKILL.md`: the root-first bootstrap, commissioning.
- `SETUP.md` §2 and §4: the Pi's real address, OS, "first run 2026-10-0x".
- `hardware/BOM.md`: the Pi 4 timing line under "not verified".
- `TODO.md` → *Open questions for Nick*: the ones under *Open* below, plus anything new.
- Memory: one entry for the Pi (address, user, what was verified).

---

## The commissioning interface (spec — to build in Part 2 step 4)

**What the commissioner sees.** Power the box on next to the robot. A Wi-Fi network
`perceptronics-setup-XXXX` appears; join it with a phone, open `http://10.42.0.1`. One
page: the wired link's state, **DHCP / Static** (address, prefix, optional gateway), the
robot's address, the camera's status, **Apply**, **Finish**. Finish turns the radio off.

| | Decision | Why |
| --- | --- | --- |
| When the hotspot is up | Only while the box is **uncommissioned** (fresh install) or **re-armed**. Finish, or a timeout (30 min with no client), ends it and blocks the radio (`rfkill`), persisted across reboots. | "During commissioning only." A box in service has no radio on. |
| Re-arming | `sudo perceptronics-setup arm` over the wired link, and a button in the cockpit page (wired side). A recovery path with **no** working wired link is needed too — see *Open*. | The usual reason to re-commission is that the wired address is wrong, which is exactly when SSH is gone. |
| Wi-Fi security | WPA2, passphrase unique per unit (generated at install, printed by the installer and `perceptronics-setup show` for the label). Never an open network. | The page changes system network config; an open AP would hand that to anyone in the building. |
| What the page can change | `eth0` DHCP/static, and `UR_HOST` (which also sets the firewall's cell subnet). Nothing else. | Smallest root-privileged surface. Tool length, hand-eye etc. stay in the cockpit / `cell.env`. |
| Privilege split | The web process runs unprivileged; a small root helper takes a validated request (strict IPv4/prefix parsing with `ipaddress`, no shell interpolation) and writes the network file. | A web form must not be a root shell. |
| Apply is safe by construction | Changes are made to `eth0` while the phone is on `wlan0`, so a wrong address never cuts off the page. The page shows the result (link, address, robot ping, Dashboard banner) before Finish is offered. | No lock-out during commissioning. |
| Network backend | Whatever owns `eth0` on the Debian image (step 1 finds out); one backend, not both. | Don't add NetworkManager to a minimal box to set one address. |
| Access point | `hostapd` + a DHCP server for the phone. Prefer systemd-networkd's built-in `DHCPServer=` over adding `dnsmasq` if networkd is what the image uses. Verify on the board; don't ship guessed configs. | Fewest new packages. |
| Firewall | `nftables.conf` gains: DHCP + the setup port, **on `wlan0` only**. No forwarding between `wlan0` and `eth0` (the forward chain already drops). | The hotspot is a door to the setup page, not to the robot network. |
| Code | Stdlib only (`http.server`), like the cockpit. A `perceptronics setup` subcommand + a `perceptronics-setup.service`, installed by `install.sh`. | Zero-dependency runtime stays zero. |
| Tests | Form parsing and file rendering under `hypothesis`; auth-free endpoints reject everything but the two fields; the helper refuses anything it didn't validate itself; log lines carry no passphrase and no raw form input. | Repo testing rules. |

**Open** (Nick's calls; defaults above are what gets built otherwise):

1. **Re-arming with no wired access** — power-cycle three times within a minute? A jumper
   across two GPIO pins at boot? A file on a USB stick? Pick one; the jumper is the least
   likely to trigger by accident.
2. **Before commissioning, what does `eth0` do?** Proposed: DHCP, falling back to nothing —
   the hotspot is the way in.
3. **Shipped units and "always offline":** the first install needs internet once. A
   pre-built SD image (flash and go) or an offline bundle (`.deb`s + a built
   `/opt/librealsense-2.58.4`) — which? Extends the 2026-09-28 question in `TODO.md`.
4. **bookworm → trixie:** stay on bookworm (LTS, restart back-off ignored), or upgrade in
   place once the baseline works?

### Not in this session (needs the robot and Nick at the pendant)

1. UR3e powered, `sudo perceptronics-doctor` green on `robot.*`.
2. Pendant: remove RealSense Pilot, install Perceptronic 0.6.0 from the USB stick,
   **Installation → URCaps → Perceptronic → Cockpit = `http://192.168.3.20:7621` → Save**.
3. Hand-eye on the Pi (`perceptronics calibrate --apply`), then delete the
   `PERCEPTRONICS_T_FLANGE_CAMERA` line from `/etc/perceptronics/cell.env` and restart —
   the environment value beats the saved file.
4. The pick kit's first run on the cell (`TODO.md`, 2026-09-28 entry).
