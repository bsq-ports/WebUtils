# Android network integration tests

Build and package WebUtils first with `qpm2 qpkg --qmod`. The probe links against
that library and runs on an ADB-connected ARM64 Android device. Python serves
local fixtures through temporary `adb reverse` connections.

Requirements: Python 3.10+, OpenSSL, ADB, Android NDK r27, and:

```sh
python -m pip install -r tests/network/requirements.txt
python tests/network/run.py --ndk /path/to/android-ndk
```

Use `--adb`, `--serial`, and `--openssl` to select specific tools or a device.
The public HTTPS check also requires an internet connection on the device.

## In-game execution

```sh
python tests/network/run.py --ndk /path/to/android-ndk --in-game
```

This mode is for a rooted Quest test headset with Beat Saber, Scotland2, WebUtils,
and the same published libcurl installed. It stops and restarts Beat Saber,
temporarily replaces WebUtils with the library extracted from `WebUtils.qmod`,
and loads a small test mod. It verifies the loaded library hash and restores the
original native files afterwards. Existing mod-manager package manifests are
not changed. If the runner is forcibly terminated, restore the library from
`build/network-tests/in-game-backup` and remove `libwebutils-network-probe.so`
from the modloader's `mods` directory before restarting the game.

## Coverage

The 42 checks cover HTTP/1.1, default HTTP/2 GET/POST and concurrency, TLS 1.2/1.3
fallback, verified public HTTPS, untrusted certificate rejection, IPv6, gzip,
deflate, Brotli, zstd, redirects, binary and empty responses, errors, query
escaping, headers, user agents, synchronous and asynchronous requests, callbacks,
progress reporting, timeouts, local-file transfers, parser failures, rate limits,
and retries. The HTTP/2 server records the negotiated protocol; the probe also
checks the HTTP/2 response status line.

Results, request traces, and in-game mapping evidence are written under
`build/network-tests`. Tests are opt-in and do not require a headset for normal
package CI. Optional Beat Saber JSON, texture, sprite, and BSML response adapters
are not exercised by this core network suite.
