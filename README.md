# thinkbook-fanctl

Read fan telemetry and run bounded manual-control sessions on the Lenovo
ThinkBook 16 G6+ AHP. Currently verified on **DMI model 21LG, BIOS NLCN37WW**.
Other models and BIOS versions are rejected before any ACPI request.

The two fans have separate RPM targets but share the EC's manual/automatic
selector. Manual sessions therefore require both targets and request automatic
control for both fans on completion, error, or handled interruption.

## Installation

Requires Linux, Python 3.14+, and `acpi_call` for the running kernel.
On Arch Linux, use `acpi_call` for `linux`, `acpi_call-lts` for `linux-lts`,
or `acpi_call-dkms` with the matching kernel headers. Kernel modules are
installed through the distribution's package manager, separately from uv.

From this repository:

```sh
make install
```

This installs the `thinkbook-fanctl` entry point, normally in `~/.local/bin`.
The install target reads `.python-version` and selects an already installed
system Python; on the tested machine this is `/usr/bin/python3.14`. uv's tool
installer ignores project Python pins unless the interpreter is selected
explicitly.
There are no third-party Python runtime dependencies. Installing the package
does not start monitoring or change fan speeds. Chezmoi is not involved.
The install target precompiles bytecode as your user. This avoids a later
`sudo` invocation creating root-owned `__pycache__` files that can block
the next `uv tool install --force`.

If `/proc/acpi/call` is missing, load the module:

```sh
sudo modprobe acpi_call
```

## Usage

ACPI queries require writing `/proc/acpi/call`, so reading telemetry also
requires root. Use the installed executable's path with sudo:

```sh
# Auto/manual mode, current and target RPM, drive bytes, flags, and WQA5 limits.
sudo ~/.local/bin/thinkbook-fanctl status
sudo ~/.local/bin/thinkbook-fanctl status --json

# Optional foreground monitoring; Ctrl+C exits. It is never started automatically.
sudo ~/.local/bin/thinkbook-fanctl watch --interval 2 --json

# Set both targets to 3000 RPM for five seconds, then request automatic control.
sudo ~/.local/bin/thinkbook-fanctl set 3000 3000 --seconds 5

# Immediately request automatic control for both fans.
sudo ~/.local/bin/thinkbook-fanctl auto

thinkbook-fanctl --help
thinkbook-fanctl --version
```

No subcommand defaults to `status`. The manual duration defaults to five
seconds. JSON monitoring output contains one object per sample; status JSON
is general telemetry, not the Waybar custom-module `text`/`tooltip` format.
The table prints `Control: auto` or `Control: manual`; JSON includes
`control_mode` and `control_mode_raw`. The mode is read from the EC, so
`stage: auto_requested` can be checked independently in the same output.

RPM values must be multiples of 100. Both CLI targets are constrained to the
limits WQA5 reports for fan 1 (2200–4500 RPM on the tested machine); these
are not independently established limits for fan 2. Use `auto`, not a zero
target, to return control to the firmware.

Request/response pairs are serialized between this utility's processes.
A second manual session is rejected; reads and `auto` remain available.
An external `auto` request changes the targets, causing a manual session to
exit at its next target check and request automatic control again. Older
diagnostic scripts and other ACPI clients do not share these locks.

Manual cleanup handles SIGINT, SIGTERM, and SIGHUP and retries automatic
control up to three times. SIGKILL and a hung process cannot run cleanup.
If all automatic-control requests fail, reboot to reset control.

## Protocol and hardware validation

The ACPI WMAE SET branch for fan 2 is a stub. Disassembly of the ITE5507 EC
executable from the official NLCN37WW package identified the missing setter:

| ERCD buffer | Action |
| --- | --- |
| `46 85 NN 00 00 00` | Fan 1 target, `NN * 100` RPM |
| `46 86 NN 00 00 00` | Fan 2 target, `NN * 100` RPM |
| `46 84 00 00 00 00` | Automatic control for both fans |

Fan 1 is set through the working WMAE interface. Fan 2 uses the six-byte
ERCD buffer. Cleanup uses WMAE SET fan 1 = 0, which sends the global auto
command. The EC clears a global automatic-mode flag for either manual setter.

FANG indices `0x8104` / `0x8204` read current RPM divided by 100;
`0x8125` / `0x8225` read the targets. These matched nonzero WMAE readings
of 2200 / 1900 RPM. Drive bytes are at offset `0x02`, flags at `0x06`.
Flags are not independent automatic-mode booleans.

The fixed read-only ERCD query `B3 04 AB 00 00 00` reads EC RAM byte
`0x04ab`. Bit 3 is the shared automatic-control flag. On NLCN37WW it was
observed as `0x08` in auto, `0x00` after setting both fans to 3000 RPM,
and `0x08` immediately after requesting auto again.

The preceding diagnostic scripts verified both fans at a target of 3000 RPM:
2700 at 1 s, 3100 at 2 s, and 3000 at 3–5 s. After the auto request, both
targets and measured speeds returned to zero. Different simultaneous targets,
other speeds, longer sessions, thermal emergency overrides, and suspend/resume
have not been tested on hardware. Package tests simulate these interfaces;
they do not establish physical behavior for additional targets.

Sources: [Lenovo BIOS package](https://think.lenovo.com.cn/support/driver/driverdetail.aspx?DEditid=146557)
and [upstream discussion of the Fan2S stub](https://lists.openwall.net/linux-kernel/2026/01/16/1362).

## Development

```sh
make                    # grouped target list
make test               # unittest suite; no EC access
make test ARGS='-k signal -v'
make lint               # Ruff and pre-commit checks
make lint.pre-commit    # workflow, Renovate, metadata, and secret checks
make format             # format sources
make lint.fix           # format and apply safe Ruff fixes
make build              # sdist and wheel in dist/
make install            # install/refresh the CLI with uv tool
make install ARGS=--editable
```

Individual checks are available as `lint.ruff` and `lint.format`; tests also
have a `test.unittest` target. Override `PYTHON`, `RUFF_VERSION`, `RUFF_CMD`,
`PRE_COMMIT_VERSION`, or `PRE_COMMIT_CMD` when needed. `ARGS` is forwarded to
the selected test, build, install, or Ruff-check command. `format` and
`lint.format` do not consume it.

Tests use a simulated EC; they do not open `/proc/acpi/call` or change fan speeds.
GitHub Actions runs the lint, test, and package-build targets on pushes to
`main` and pull requests. Renovate checks dependencies and tooling weekly.

## License

MIT; see [LICENSE](LICENSE). This license covers this project's source code,
not Lenovo firmware or BIOS images.
