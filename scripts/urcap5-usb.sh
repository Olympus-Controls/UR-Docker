#!/usr/bin/env bash
# Put the current PolyScope 5 URCap on a USB stick for the pendant, safely:
# no macOS "._" AppleDouble files (PolyScope's URCap picker would list
# "._perceptronic-….urcap" and fail on it), checksum-verified, then ejected.
#
#   scripts/urcap5-usb.sh                 # the stick named "URE MODELS"
#   scripts/urcap5-usb.sh "MY STICK"      # another FAT32 stick
#
# Then on the pendant: ☰ → Settings → System → URCaps → + → the file → Open → Restart.
set -euo pipefail
cd "$(dirname "$0")/.."

volume="/Volumes/${1:-URE MODELS}"
urcap="$(find urcap/dist -maxdepth 1 -name 'perceptronic-ps5-*.urcap' | sort | tail -1)"
[[ -d "$volume" ]] || { echo "no stick at $volume — plug it in (or pass its name)"; exit 1; }
[[ -f "$urcap" ]] || { echo "no $urcap — make urcap5-package"; exit 1; }

# replace any earlier build of this URCap on the stick
find "$volume" -maxdepth 1 \( -name 'perceptronic-ps5-*.urcap' -o -name '._perceptronic-ps5-*' \) -delete
cp -X "$urcap" "$volume/"
dot_clean -m "$volume" 2>/dev/null || true
sync

want="$(shasum -a 256 "$urcap" | cut -d' ' -f1)"
got="$(shasum -a 256 "$volume/$(basename "$urcap")" | cut -d' ' -f1)"
[[ "$want" == "$got" ]] || { echo "checksum mismatch on the stick — try again"; exit 1; }
if find "$volume" -maxdepth 1 -name '._*' | grep -q .; then
  echo "warning: ._ files remain on the stick"
fi
echo "copied $(basename "$urcap") (sha256 ${want:0:12}…) to $volume"
diskutil eject "$volume" >/dev/null && echo "ejected — take it to the pendant"
