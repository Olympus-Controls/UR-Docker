F=/usr/libexec/ApplicationFirewall/socketfilterfw
P=~/.local/share/uv/python/cpython-3.13.5-macos-aarch64-none/bin/python3.13
sudo $F --remove "$P"; sudo $F --add "$P"; sudo $F --unblockapp "$P"
sudo $F --setglobalstate on

