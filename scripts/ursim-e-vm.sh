#!/usr/bin/env bash
# The e-Series URSim (amd64-only image) on an Apple-silicon Mac, in a full x86_64
# QEMU virtual machine running Docker — the same `universalrobots/ursim_e-series`
# container the PolyScope X sims run natively, one emulation layer down.
#
# Why a VM: Docker Desktop emulates amd64 per process, and neither of its emulators
# carries this image — Rosetta traps Xvfb, QEMU user-mode kills URControl
# (TODO.md 2026-09-04, CLAUDE.md gotchas). A whole emulated x86_64 machine is a
# different path: slow (every instruction is translated), but nothing is
# user-mode-emulated.
#
# Status (2026-09-27, M1 Max): URControl, Dashboard, Primary/RTDE and PolyScope's
# URCap loader work — a URCap bundle resolves and registers (that is how the e-Series
# URCap's first PolyScope 5 bug was found). PolyScope's JVM is NOT stable here: JIT
# code crashes (SIGILL/SIGSEGV, hs_err_pid*.log in /ursim/GUI) and startup NPEs on
# some boots, with -cpu max and with Nehalem, multi- and single-threaded TCG. Use it
# to check that a URCap loads, not for clicking through PolyScope.
#
#   scripts/ursim-e-vm.sh up                 create (first run) + boot the VM, start URSim
#   scripts/ursim-e-vm.sh status             VM, container and Dashboard state
#   scripts/ursim-e-vm.sh install-urcap F    copy a .urcap in and restart URSim to load it
#   scripts/ursim-e-vm.sh log [N]            tail polyscope.log inside the container
#   scripts/ursim-e-vm.sh ssh [CMD...]       a shell (or one command) in the VM
#   scripts/ursim-e-vm.sh down               stop the VM (the disk is kept)
#   scripts/ursim-e-vm.sh destroy            delete the VM disk (the base image is kept)
#
# Ports are forwarded to 127.0.0.1 exactly as docker-compose's e-Series service does:
# Dashboard 29999, Primary 30001, 30002-30004, noVNC 6080 — so `urctl state` works
# unchanged. Override with URSIM_E_* env vars below.
set -euo pipefail

VM_DIR="${URSIM_E_VM_DIR:-$HOME/.cache/ur-utils/ursim-e-vm}"
IMAGE="${URSIM_E_IMAGE:-universalrobots/ursim_e-series:5.26.0}"
MODEL="${URSIM_E_MODEL:-UR3}"
CPUS="${URSIM_E_CPUS:-6}"
# single: every vCPU on one host thread. Multi-threaded TCG only approximates x86's
# strong memory ordering on ARM, and PolyScope's OSGi resolver (Felix, parallel) then
# NPEs on 2 of 4 boots (2026-09-27: HashMap.get(null) in ResolverImpl → Guice
# "No implementation … was bound", no Dashboard). "multi" is faster when it works.
TCG_THREAD="${URSIM_E_TCG_THREAD:-single}"
# No AVX: with `-cpu max` HotSpot (PolyScope's JRE 8) JIT-compiles AVX/AVX2 code that
# TCG doesn't execute faithfully — SIGILL / SIGSEGV in compiled frames and a
# PolyScope that dies when a screen opens (2026-09-27, hs_err_pid*.log). Nehalem is
# SSE4.2 without AVX, and real e-Series control boxes have no AVX either.
CPU_MODEL="${URSIM_E_CPU:-Nehalem-v2}"
MEM_MB="${URSIM_E_MEM_MB:-8192}"
DISK_GB="${URSIM_E_DISK_GB:-40}"
SSH_PORT="${URSIM_E_SSH_PORT:-2222}"
BASE_URL="https://cloud-images.ubuntu.com/noble/current"
BASE_IMG="noble-server-cloudimg-amd64.img"
PORTS=(29999 30001 30002 30003 30004 6080)

say() { printf '[ursim-e-vm] %s\n' "$*"; }
die() { printf '[ursim-e-vm] %s\n' "$*" >&2; exit 1; }

need() {
  command -v "$1" >/dev/null 2>&1 || die "$1 not found — $2"
}

vm_ssh() {
  ssh -i "$VM_DIR/id_ed25519" -p "$SSH_PORT" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
    -o ConnectTimeout=10 ur@127.0.0.1 "$@"
}

vm_running() {
  [[ -f "$VM_DIR/qemu.pid" ]] && kill -0 "$(cat "$VM_DIR/qemu.pid")" 2>/dev/null
}

fetch_base() {
  mkdir -p "$VM_DIR"
  if [[ -f "$VM_DIR/$BASE_IMG" ]]; then return; fi
  say "downloading the Ubuntu 24.04 amd64 cloud image (~600 MB)…"
  curl -fL --retry 3 -o "$VM_DIR/$BASE_IMG.part" "$BASE_URL/$BASE_IMG"
  curl -fsSL -o "$VM_DIR/SHA256SUMS" "$BASE_URL/SHA256SUMS"
  local want got
  want="$(grep " \*$BASE_IMG\$" "$VM_DIR/SHA256SUMS" | cut -d' ' -f1)"
  got="$(shasum -a 256 "$VM_DIR/$BASE_IMG.part" | cut -d' ' -f1)"
  [[ -n "$want" && "$want" == "$got" ]] || die "checksum mismatch for $BASE_IMG (want $want, got $got)"
  mv "$VM_DIR/$BASE_IMG.part" "$VM_DIR/$BASE_IMG"
}

make_seed() {
  [[ -f "$VM_DIR/id_ed25519" ]] || ssh-keygen -q -t ed25519 -N "" -C ursim-e-vm -f "$VM_DIR/id_ed25519"
  local seed="$VM_DIR/seed"
  rm -rf "$seed" "$VM_DIR/seed.iso"
  mkdir -p "$seed"
  cat >"$seed/meta-data" <<EOF
instance-id: ursim-e-vm
local-hostname: ursim-e-vm
EOF
  cat >"$seed/user-data" <<EOF
#cloud-config
users:
  - name: ur
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $(cat "$VM_DIR/id_ed25519.pub")
packages:
  - docker.io
runcmd:
  - usermod -aG docker ur
  - systemctl enable --now docker
  - mkdir -p /home/ur/urcaps
  - chown ur:ur /home/ur/urcaps
EOF
  hdiutil makehybrid -quiet -iso -joliet -default-volume-name cidata -o "$VM_DIR/seed.iso" "$seed"
}

boot() {
  local fwd="hostfwd=tcp:127.0.0.1:$SSH_PORT-:22"
  for p in "${PORTS[@]}"; do fwd="$fwd,hostfwd=tcp:127.0.0.1:$p-:$p"; done
  qemu-system-x86_64 \
    -machine q35 -accel "tcg,thread=$TCG_THREAD,tb-size=1024" -cpu "$CPU_MODEL" \
    -smp "$CPUS" -m "$MEM_MB" \
    -drive "file=$VM_DIR/disk.qcow2,if=virtio" \
    -drive "file=$VM_DIR/seed.iso,media=cdrom,readonly=on" \
    -netdev "user,id=n0,$fwd" -device virtio-net-pci,netdev=n0 \
    -display none -serial "file:$VM_DIR/serial.log" \
    -daemonize -pidfile "$VM_DIR/qemu.pid"
}

wait_for() {  # wait_for SECONDS DESCRIPTION CMD...
  local limit=$1 what=$2 t0=$SECONDS
  shift 2
  until "$@" >/dev/null 2>&1; do
    (( SECONDS - t0 > limit )) && die "timed out after ${limit}s waiting for $what (serial log: $VM_DIR/serial.log)"
    sleep 10
  done
  say "$what after $(( SECONDS - t0 ))s"
}

container_up() {
  vm_ssh "docker ps --format '{{.Names}}' | grep -qx ursim"
}

start_ursim() {
  if vm_ssh "docker ps -a --format '{{.Names}}' | grep -qx ursim"; then
    vm_ssh docker start ursim >/dev/null
    return
  fi
  say "pulling $IMAGE inside the VM (slow under emulation)…"
  vm_ssh docker pull "$IMAGE"
  local pub=""
  for p in "${PORTS[@]}"; do pub="$pub -p $p:$p"; done
  # shellcheck disable=SC2086  # $pub is a list of -p flags, built above
  vm_ssh docker run -dit --name ursim --restart unless-stopped \
    --security-opt seccomp=unconfined --cap-add NET_BIND_SERVICE \
    -e ROBOT_MODEL="$MODEL" -v /home/ur/urcaps:/urcaps $pub "$IMAGE" >/dev/null
}

dashboard_ready() {
  python3 - <<'EOF'
import socket, sys
try:
    s = socket.create_connection(("127.0.0.1", 29999), 3)
    s.settimeout(5)
    banner = s.recv(256).decode(errors="replace")
    s.sendall(b"robotmode\n")
    reply = s.recv(256).decode(errors="replace")
    s.close()
    sys.exit(0 if "Robotmode:" in reply and "NO_CONTROLLER" not in reply else 1)
except OSError:
    sys.exit(1)
EOF
}

cmd_up() {
  need qemu-system-x86_64 "brew install qemu"
  need qemu-img "brew install qemu"
  need hdiutil "macOS only"
  if vm_running; then
    say "VM already running (pid $(cat "$VM_DIR/qemu.pid"))"
  else
    fetch_base
    if [[ ! -f "$VM_DIR/disk.qcow2" ]]; then
      qemu-img create -q -f qcow2 -F qcow2 -b "$VM_DIR/$BASE_IMG" "$VM_DIR/disk.qcow2" "${DISK_GB}G"
    fi
    make_seed
    say "booting an emulated x86_64 VM ($CPU_MODEL ×$CPUS, TCG thread=$TCG_THREAD, $MEM_MB MB) — first boot takes minutes"
    boot
  fi
  wait_for 1800 "ssh" vm_ssh true
  wait_for 3600 "cloud-init (docker install)" vm_ssh cloud-init status --wait
  wait_for 600 "docker" vm_ssh docker info
  container_up || start_ursim
  wait_for 3600 "the URSim Dashboard on 127.0.0.1:29999" dashboard_ready
  say "URSim is up: urctl state · noVNC http://127.0.0.1:6080/vnc.html"
}

cmd_status() {
  if vm_running; then say "VM running (pid $(cat "$VM_DIR/qemu.pid"))"; else say "VM not running"; return 0; fi
  vm_ssh "docker ps -a --filter name=ursim --format '{{.Names}} {{.Status}}'" || say "ssh not answering yet"
  if dashboard_ready; then say "Dashboard answering on 127.0.0.1:29999"; else say "Dashboard not answering yet"; fi
}

cmd_install_urcap() {
  local file=${1:-}
  [[ -f "$file" ]] || die "usage: $0 install-urcap path/to/file.urcap"
  vm_running || die "VM not running — $0 up"
  local name
  name="$(basename "$file" .urcap).jar"  # the image's entrypoint copies /urcaps/*.jar into PolyScope's bundles
  scp -i "$VM_DIR/id_ed25519" -P "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR "$file" "ur@127.0.0.1:/home/ur/urcaps/$name"
  say "restarting URSim to load ${name}…"
  vm_ssh docker restart ursim >/dev/null
  wait_for 3600 "the URSim Dashboard on 127.0.0.1:29999" dashboard_ready
  vm_ssh "docker exec ursim sh -c 'grep -i -E \"urcap|realsense|olympus\" /ursim/polyscope.log | tail -20'" || true
}

cmd_log() {
  vm_ssh docker exec ursim tail -n "${1:-60}" /ursim/polyscope.log
}

cmd_down() {
  if ! vm_running; then
    say "VM not running"
    return 0
  fi
  local pid t0=$SECONDS
  pid="$(cat "$VM_DIR/qemu.pid")"
  vm_ssh sudo poweroff >/dev/null 2>&1 || kill "$pid"
  # wait for QEMU itself to exit — `up` right after a poweroff would find it alive
  while kill -0 "$pid" 2>/dev/null; do
    if (( SECONDS - t0 > 120 )); then kill "$pid" 2>/dev/null || true; fi
    sleep 2
  done
  rm -f "$VM_DIR/qemu.pid"
  say "VM stopped after $(( SECONDS - t0 ))s"
}

cmd_destroy() {
  cmd_down
  rm -f "$VM_DIR/disk.qcow2" "$VM_DIR/seed.iso" "$VM_DIR/qemu.pid" "$VM_DIR/serial.log"
  say "VM disk deleted (base image kept in $VM_DIR)"
}

case "${1:-}" in
  up) cmd_up ;;
  status) cmd_status ;;
  install-urcap) shift; cmd_install_urcap "$@" ;;
  log) shift; cmd_log "$@" ;;
  ssh) shift; vm_ssh "$@" ;;
  down) cmd_down ;;
  destroy) cmd_destroy ;;
  *) sed -n '2,23p' "$0"; exit 2 ;;
esac
