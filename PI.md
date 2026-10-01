# PI.md — bringing up the Raspberry Pi 5 pick PC

The Pi 5 arrives **Thursday 2026-10-01**. Part 1 is for Nick (hands on the board, ~20 min).
Part 2 is for the Claude session (Fable) that does the rest over SSH from the Mac Studio.
The reference for what the installer does is `deploy/pi/README.md`; the procedure is the
`deploy-pick-pc` skill. This page is the first-run plan on top of them, and it settles the
pick-PC questions `TODO.md` defers to it.

**Nothing in `deploy/pi/` has run on a board yet** (written 2026-09-28, CI-checked only).
Thursday is the first run: expect to find things, and write them down (Part 2, step 8).

## Decisions (Nick, 2026-09-30)

| | |
| --- | --- |
| Board | **Raspberry Pi 5.** |
| OS | **Debian 13 (trixie)** — open, non-proprietary, current. See *Debian on a Pi 5* for what that can and can't mean. |
| Network in service | **Offline, one Ethernet cable to the robot.** No Wi-Fi client, no internet at runtime. Internet during setup is fine. |
| Wired address | The commissioner's choice, **DHCP or static**, from a **web page over a hotspot** the box raises during commissioning only. |
| Stuck? | **Power-cycle it.** A box that can't reach its robot after a power-on raises the hotspot again. No jumpers, no SSH. |
| How units are made | **A pre-built SD image**: flash, boot, commission. Nothing compiles or downloads on a shipped unit. |
| librealsense | Built once, into the image, with the fewest fetched dependencies that still pass the binding's checks (`TODO.md`: "whichever reduces dependencies and increases portability"). |
| Hand-eye in `cell.env` | **Not in the image.** A unit's hand-eye comes from its own calibration file; an environment line would silently beat it. |

## Debian on a Pi 5 (checked 2026-09-30)

- **Debian's own images don't run a Pi 5 on a stable release.** The Debian wiki
  (`RaspberryPiImages`, modified 2026-08-17): *"Raspberry Pi 5 is only supported in upcoming
  Debian forky (debian-14-raspi-arm64 or in current sid)."* raspi.debian.net's images stop
  at the Pi 4 and at 2023–24. So "pure Debian" on this board means **testing**, not trixie.
- **Trixie on a Pi 5 needs Raspberry Pi's kernel and firmware packages.** The kernel is
  their GPL fork; the boot firmware and the Wi-Fi blob are closed on every OS, Debian's
  included.
- **The product image: Debian trixie assembled with `rpi-image-gen`** (Raspberry Pi's
  image builder, BSD-3-Clause, declarative YAML + layers + hooks, Debian packages plus the
  Pi kernel/firmware). It carries only what we list — not Raspberry Pi OS's userland — and
  it is the same tool that produces the flash-and-go image. Native build hosts are arm64
  Debian bookworm/trixie, i.e. the Pi itself.
- **Thursday's first card is Raspberry Pi OS Lite (64-bit), 2026-09-15** — trixie, kernel
  6.18. It is scaffolding: a known-good trixie on this board to prove the deploy against and
  to build the product image on. It is not what ships.
- Worth one look later, on a spare card: Debian's forky image, to see how far the fully
  Debian kernel gets with the D435 on USB 3. Not Thursday.

---

## Part 1 — Nick, by hand

### On the bench

- The Pi 5, its **active cooler** (the librealsense compile runs all cores for a long
  time), a microSD ≥ 32 GB and a reader. **A second card** if you have one, for the product
  image.
- **The 27 W (5.1 V / 5 A) USB-C PD supply.** On a lesser supply a Pi 5 caps all USB ports
  at 600 mA total, and the D435 is powered from that budget. Not yet tested here either way.
- The D435 and a short **USB 3** C-to-A cable, no hub.
- An Ethernet cable to the **office** network (the one the Mac Studio's `10.0.0.16` is on).
- No monitor or keyboard.

### 1. Flash

Raspberry Pi Imager → device **Raspberry Pi 5** → **Raspberry Pi OS (other) → Raspberry Pi
OS Lite (64-bit)** → the card. In the customisation step:

| Setting | Value |
| --- | --- |
| Hostname | `pickpc` |
| Username / password | `nick` / anything (console login only) |
| Wireless LAN | **leave empty** |
| SSH | enabled, **public-key only**, key = the Mac Studio's |

The key is the Mac Studio's, because the deploy runs from there:

    cat ~/.ssh/id_ed25519.pub        # on the Mac Studio; fingerprint SHA256:Xz1K1SaS…nzzDI

### 2. Assemble and boot

1. Cooler on, card in, **D435 into a blue (USB 3) port**, **Ethernet to the office
   network**.
2. Power on with the 27 W supply. First boot resizes the card and reboots: two minutes.

### 3. Check you can get in (from the Mac Studio)

    ssh nick@pickpc.local 'hostname -I; uname -m; sudo -n true && echo sudo-ok'

Expect a `10.0.0.x` address, `aarch64`, `sudo-ok`.

- Name doesn't resolve: the router's lease list, or
  `ping -c1 10.0.0.255 >/dev/null; arp -a | grep -i -E '2c:cf:67|d8:3a:dd|88:a2:9e|dc:a6:32|e4:5f:01'`.
- No `sudo-ok`: once, on the Pi —
  `echo "nick ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/010-nick-nopasswd && sudo chmod 0440 /etc/sudoers.d/010-nick-nopasswd && sudo visudo -c`
- Host-key warning after a re-flash: `ssh-keygen -R pickpc.local` and `-R <ip>`.

### 4. Hand over

    Pi address on the office LAN: 10.0.0.___

Prompt for Fable: *"The Pi 5 is up at nick@10.0.0.___ — follow PI.md part 2."*

Don't upgrade, install or configure anything on it. You come back twice: to flash the
product image (step 6) and, with a phone, to commission it (step 7).

---

## Part 2 — Fable, over SSH

You are on the Mac Studio in this checkout. Load the **`deploy-pick-pc`** skill; the steps
below are the first-run specifics it doesn't know. Use the Pi's **IP address**, never
`pickpc.local`: the installer's firewall drops inbound mDNS, so the name stops resolving
when the deploy finishes.

Order matters: **baseline green (1–3) → commissioning interface (4) → image (5) → flash
and commission with Nick (6–7).** Don't build new things on a box whose baseline you
haven't seen work.

### 0. Before touching the Pi

- Session-start audit. Nick edits `TODO.md` by hand in this tree — an `M TODO.md` you
  didn't make is his; leave it, stage by path.
- Fresh branch off `fork/dev` for deploy fixes; steps 4 and 5 are feature branches of
  their own (separate worktrees if you run them in parallel).
- `env -u UR_CELL uv run pytest tests/test_deploy_pi.py -q` green before you start.

### 1. Preflight

The skill's table, plus:

    ssh nick@<ip> '. /etc/os-release; echo $ID $VERSION_CODENAME; uname -r; python3 -V; systemctl --version | head -1'
    ssh nick@<ip> 'cat /proc/device-tree/model; free -m; df -h / /var/tmp; nproc'
    ssh nick@<ip> 'vcgencmd get_throttled; vcgencmd measure_temp; vcgencmd get_config usb_max_current_enable'
    ssh nick@<ip> 'nmcli -t -f NAME,TYPE,DEVICE con show 2>/dev/null; ls /etc/netplan /etc/systemd/network 2>/dev/null; ip -br addr; rfkill list'

- Expect `trixie`, systemd ≥ 254 (the unit's restart back-off works), Python 3.13.
- `get_throttled` must be `0x0`. `usb_max_current_enable=0` means the supply did not
  negotiate 5 A and USB is capped at 600 mA: tell Nick before blaming the camera.
- D435 link speed (the skill's sysfs one-liner): `5000` = USB 3.
- Note what manages the network (NetworkManager, netplan, networkd) — but the product
  image's answer is what step 4 builds against, not this card's.

### 2. Deploy

    scripts/deploy-pi.sh nick@<ip> --cell ur3 --robot-host 192.168.3.3

- **Background, 2 h limit, poll.** The build has never been timed on a board. `install.sh`
  sizes jobs as RAM / 1536 MiB. Watch:
  `ssh nick@<ip> 'tail -n 3 /var/tmp/perceptronics-build/cmake-build.log; vcgencmd measure_temp'`.
- A killed run is safe to repeat but **starts the build from scratch**. Two failed
  attempts → stop and describe.
- The firewall admits :7621/:7622 from `192.168.3.0/24` only, so on the office LAN the
  cockpit is reached through a tunnel. SSH stays open.
- Note the wall-clock build time.

### 3. Verify the baseline

    scripts/deploy-pi.sh nick@<ip> --doctor-only
    ssh nick@<ip> 'systemctl is-active perceptronics-cockpit; journalctl -u perceptronics-cockpit -b -n 40 --no-pager'
    ssh nick@<ip> 'sudo nft list table inet perceptronics; ss -ltn | grep -E ":762[12]"'
    ssh -f -N -L 7621:127.0.0.1:7621 nick@<ip> && curl -s http://127.0.0.1:7621/api/info | head -c 600

(Kill the tunnel after: `pkill -f "7621:127.0.0.1:7621"`.)

Good, with the robot unplugged: `sdk` ok; `camera … usb 3.x`; `cockpit … fps ~30` with
`seq` advancing. The `robot.*` lines **fail** — expected, report them verbatim.

Each of the README's *Open items* gets a yes/no from this run:

| Open item | How to answer it |
| --- | --- |
| librealsense builds on arm64 Debian outside Docker | the deploy finished; note the minutes |
| `nft -c` accepts the rendered firewall | `nft list table` shows the table |
| the unit is valid | `systemd-analyze verify /etc/systemd/system/perceptronics-cockpit.service` |
| the **hardened, non-root** unit opens the D435 | frames advancing; if not, `DeviceAllow=` / `DevicePolicy=` first |
| hardening score | `systemd-analyze security perceptronics-cockpit` |
| the D435 holds on the Pi 5's USB budget | 10 min streaming: `seq` advancing, `get_throttled` still `0x0`, no `Frame didn't arrive` |
| per-frame cost | `curl -s -o /dev/null -w '%{time_total}\n' http://127.0.0.1:7621/api/color.png` ×10; `top` for the cockpit |

Then the librealsense dependency question: read `CMake/lrs_options.cmake` in the v2.58.4
tree for what makes configure fetch nlohmann/json, fastcdr, yaml-cpp and sqlite, turn off
what the ctypes binding doesn't use, rebuild, and confirm `_check_enums`, streaming and the
filters still pass. Keep the option only if they do. Take the option names from the source,
not from memory.

### 4. Build the commissioning interface

Spec below. Draft PR: it touches `deploy/`, the firewall and a root-privileged path.

- Tests first for the pure logic (address validation, rendering the network file, the
  arm/finish state machine) — adversarial inputs; this is a web form that writes system
  configuration as root.
- New packages on the PC (an access-point daemon) get the dependency review in the PR.
- You are connected over **Ethernet** and the feature reconfigures Ethernet. Until Nick is
  at the bench, exercise apply against a **rendered file / dry run**. The first live apply
  is step 7, with Nick's phone on the hotspot as the way back in.

### 5. Build the SD image

Spec below. Build it **on the Pi** (the builder's native host), copy the `.img` to the
Mac, checksum it.

### 6. Flash the product image (Nick)

Nick writes the image to the second card (or this one, once the `.img` is safely on the
Mac) and boots it, Ethernet still on the office LAN. You verify it the way step 3 verified
the baseline — and that it did so **without fetching anything**: no apt, no git, no pip in
the journal.

### 7. Commission it for real (Nick, with a phone)

1. A fresh image is uncommissioned: the hotspot is up by itself. Nick joins it and opens
   the page.
2. **Static `192.168.3.20/24`, no gateway**, robot `192.168.3.3`, Apply. The office-LAN
   SSH session dies here — that is the feature working.
3. Nick moves the cable to the cell switch. From the Mac Studio's cell interface
   (`192.168.3.10`): `ssh nick@192.168.3.20 'ip -br addr; sudo perceptronics-doctor'`.
4. **Finish.** Confirm the radio is off (`rfkill list`) and stays off across a reboot with
   the robot reachable.
5. The stuck path: unplug the robot cable, power-cycle, confirm the hotspot returns; plug
   back in, power-cycle, confirm it doesn't.
6. Unhappy paths once each: a garbage address in the form, DHCP with no server on the
   wire, power cut mid-apply.

`192.168.3.20`, not the `.10` in `SETUP.md` and the README: `.10` is the Mac Studio.

### 8. If something breaks

- Fix it **in the repo**, regression test first, commit, redeploy. Never hand-edit the
  unit or the firewall on the Pi.
- Build failed: `/var/tmp/perceptronics-build/cmake-{configure,build}.log`. `Killed` /
  `internal compiler error` = memory; a thermal crawl shows in `measure_temp`.
- Locked out: the Pi is expendable — re-flash, 10 minutes. Nothing in this runbook sends
  robot motion, and nothing should.

### 9. Write it down, then PR

- `deploy/pi/README.md`: Pi 5 + trixie, the image as the install path (the SSH deploy
  becomes the developer/update path), commissioning, each verified *Open item* with its
  date and number, `.10` → `.20`.
- `.claude/skills/deploy-pick-pc/SKILL.md`: flashing, commissioning, the stuck path.
- `SETUP.md` §2 and §4; `hardware/BOM.md` (it names a **Pi 4** as the kit board — see
  *Open*); `TODO.md`: close the two pick-PC questions with what this page decided.
- Memory: one entry for the Pi (address, user, what was verified).

---

## Spec: the commissioning interface

**What the commissioner sees.** Flash, plug the box into the robot, power on. A Wi-Fi
network `perceptronics-setup-XXXX` appears; join it with a phone, open `http://10.42.0.1`.
One page: the wired link's state, **DHCP / Static** (address, prefix, optional gateway),
the robot's address, the camera's status, **Apply**, **Finish**.

| | Decision | Why |
| --- | --- | --- |
| When the hotspot is up | At power-on, if the box is **uncommissioned**, or it is commissioned but **cannot reach its robot on the wire** within 3 minutes (a UR controller boots slower than a Pi). It ends on Finish or after 15 minutes with no client, and the radio is then blocked (`rfkill`). | Commissioning only — and "stuck" fixes itself with a power cycle. |
| A healthy box | Never turns the radio on. | In service it is wired and offline. |
| Cost of that rule | A cell started with the robot off shows the hotspot for 15 minutes. Accepted; it is WPA2 and reaches only the setup page. The alternative (three quick power cycles) trades that for a gesture nobody remembers. | |
| Re-commissioning a working box | `sudo perceptronics-setup arm`, or a button in the cockpit (wired side). | Changing a working address on purpose. |
| Wi-Fi security | WPA2, passphrase unique per unit, generated on first boot, shown by `perceptronics-setup show` for the label. Never open. | The page changes system network config. |
| What the page can change | `eth0` DHCP/static, and `UR_HOST` (which also sets the firewall's cell subnet). Nothing else. | Smallest root-privileged surface. |
| Privilege split | The web process is unprivileged; a small root helper takes a validated request (`ipaddress`, no shell interpolation) and writes the network file. | A web form must not be a root shell. |
| Apply can't lock you out | The change is to `eth0` while the phone is on `wlan0`. The page shows the result (link, address, robot ping, Dashboard banner) before Finish is offered. | |
| Network backend | The one the product image ships, chosen for the fewest packages (systemd-networkd is already there; its `DHCPServer=` can serve the phone). | Don't add NetworkManager to set one address. |
| Access point | `hostapd` unless something already in the image does it. Verify on the board; no guessed configs. | Fewest new packages. |
| Firewall | DHCP + the setup port on `wlan0` only; no forwarding between `wlan0` and `eth0`. | The hotspot is a door to the setup page, not to the robot network. |
| Code | Stdlib only: a `perceptronics setup` subcommand + `perceptronics-setup.service`. | The zero-dependency runtime stays zero. |
| Tests | Form parsing and file rendering under `hypothesis`; the helper refuses anything it didn't validate itself; no passphrase and no raw form input in any log line; the arm/finish/stuck state machine with time mocked. | Repo testing rules. |

## Spec: the SD image

| | Decision |
| --- | --- |
| Tool | `rpi-image-gen` at a pinned commit, config + layer under `deploy/image/` in this repo. |
| Contents | Debian trixie minimal + the Pi 5 kernel/firmware + exactly the packages `install.sh` needs at **runtime** (no compilers), `/opt/librealsense-2.58.4` pre-built, the wheel's venv, the unit, the firewall, the setup service. |
| One source of truth | The image hook runs **`install.sh`** (a build-time mode that skips starting services), not a second copy of its steps. |
| Per-unit state, made on first boot | SSH host keys, machine-id, the hotspot passphrase, filesystem grow. Nothing unit-specific is baked in. |
| Not in the image | Any hand-eye line, any robot address, any SSH key of ours. `cell.env` holds the cell profile minus `UR_HOST` and `PERCEPTRONICS_T_FLANGE_CAMERA` until commissioning. |
| SSH on a shipped unit | **Off** unless an `authorized_keys` file is dropped on the boot partition before first boot. (Open, below.) |
| Updates | `scripts/deploy-pi.sh` over the wire still works for a developer; a field update is a new card. |
| Build + release | Built on an arm64 Debian host (the Pi; CI's arm64 runner if the tool accepts it), reproducible inputs pinned, attached to a GitHub Release with its sha256 — like the URCap jars. |
| Tests | The config and hook are linted in CI; the image itself is verified by booting it (step 6) until an emulated boot is worth building. |

## Open (Nick's calls; the defaults above get built otherwise)

1. **The kit BOM says Pi 4.** PR #19 moved `hardware/BOM.md`, `SETUP.md` and the README to
   a Raspberry Pi 4 4 GB as the kit board, and the BOM lists reasons against a Pi 5 (PD
   supply, 600 mA USB cap). Is the Pi 5 the kit board now, or just this unit?
2. **SSH on shipped units**: off by default as specified, or on with a per-unit key?
3. **Hotspot when the robot is simply off** for 15 minutes at each such start: fine, or
   use the triple-power-cycle gesture instead?

### Not in this session (needs the robot and Nick at the pendant)

1. UR3e powered, `sudo perceptronics-doctor` green on `robot.*`.
2. Pendant: remove RealSense Pilot, install Perceptronic 0.6.0 from the USB stick,
   **Installation → URCaps → Perceptronic → Cockpit = `http://192.168.3.20:7621` → Save**.
3. Hand-eye on the Pi: `perceptronics calibrate --apply` writes the unit's file.
4. The pick kit's first run on the cell (`TODO.md`, 2026-09-28 entry).
