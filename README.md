# ms-a2-fancontrol

Demand-based fan control for the **Minisforum MS-A2**, built for Proxmox VE and Debian hosts.

The stock fan curve of the MS-A2 only follows the CPU temperature. Under sustained disk load, the NVMe drives get hot while the system fan still idles. In our tests the NVMe controller reached 93 °C with the stock curve. The usual workaround, fixed full speed in the BIOS, keeps everything cool but is loud all the time.

`ms-a2-fancontrol` is a small systemd service that drives the fans by the temperatures that matter:

- **System fan**: speeds up with the **NVMe temperature**.
- **CPU fan**: speeds up with the **RAM temperature** (the RAM sits in its airflow) or the **CPU temperature**, whichever needs more. It still follows the firmware's CPU curve.
- Fans never spin **slower than the firmware curve**, and only as fast as needed.
- **Emergency level**: both fans go to full speed when a component gets hot, when temperatures can't be read, or when the service crashes.

With the service, the NVMe controller stayed at 77 °C or below under the same load that drove it to 93 °C with the stock curve. At idle the fans run near the firmware levels instead of full speed.

> [!WARNING]
> This tool writes to the embedded controller (EC) of your machine. It only changes the fan curve's speed levels and refuses to run on unknown firmware. Use it at your own risk, as stated in the license. The project is not affiliated with Minisforum.

## Contents

- [Supported hardware](#supported-hardware)
- [Requirements](#requirements)
- [Installation](#installation)
- [Checking that it works](#checking-that-it-works)
- [Configuration](#configuration)
- [How it works](#how-it-works)
- [Uninstalling](#uninstalling)
- [Troubleshooting](#troubleshooting)
- [Measurements](#measurements)
- [EC register map](#ec-register-map)
- [Development](#development)
- [License](#license)

## Supported hardware

| | |
|---|---|
| Device | Minisforum MS-A2 |
| BIOS | 1.02. The service refuses other versions. |
| Tested with | Ryzen 9 7945HX, 2× Micron 7450 NVMe in the onboard M.2 slots, 2× 64 GB DDR5 SO-DIMM; five units |
| OS | Proxmox VE 9 (Debian 13 "trixie"). Any Debian-based system with systemd, Python 3 and a recent kernel should work. |

At startup the service compares the product name, the BIOS version and the fan curve values in the EC with known values. If anything differs, it exits without writing. Other CPU variants of the MS-A2 (e.g. Ryzen 9 9955HX) are untested. They work only if their firmware matches. If yours doesn't, see [Other BIOS versions and CPU variants](#other-bios-versions-and-cpu-variants).

Only two NVMe drives in the onboard M.2 slots have been tested. Other drive setups, such as a third M.2 drive, U.2/U.3 drives via adapter or NVMe drives on a PCIe card, are untested. The service uses **every** NVMe drive the kernel reports:

- The hottest drive sets the system fan, even if that fan doesn't cool it (e.g. a drive on a PCIe card). The fan then speeds up without lowering that temperature, possibly up to the emergency level.
- `NVME_SENSOR1_MAX` applies to the second temperature sensor (`temp2`) of every drive. On the Micron 7450 that is the controller. Other drives may report a different sensor there or none at all.

With such setups, check the values in `ms-a2-fancontrol status` and adjust the thresholds in the [configuration](#configuration) if needed.

## Requirements

1. **Secure Boot disabled.** The EC is only reachable through the I/O ports (`/dev/port`). With Secure Boot on, kernel lockdown blocks that access, and the MS-A2 offers no other interface: no ACPI EC device, no fan or PWM entries in hwmon.
2. **BIOS fan mode "Auto".** The service raises the levels of the firmware's fan curve. With a fixed speed set in the BIOS, the EC ignores the curve.
3. **Temperature sensors** in hwmon: `nvme` (NVMe drives), `spd5118` (DDR5 modules) and `k10temp` (CPU). Proxmox VE 9 loads all of them automatically.

Check before installing:

```bash
mokutil --sb-state                       # expected: SecureBoot disabled
cat /sys/kernel/security/lockdown        # expected: [none] integrity confidentiality
cat /sys/class/dmi/id/product_name /sys/class/dmi/id/bios_version   # MS-A2, 1.02
grep -h . /sys/class/hwmon/hwmon*/name | sort | uniq -c             # nvme, spd5118, k10temp
```

## Installation

Install the package on every MS-A2 host, for example on each node of a Proxmox VE cluster.

### From a release

Download `ms-a2-fancontrol_<version>_all.deb` from the [Releases](https://github.com/proxforge/ms-a2-fancontrol/releases) page and install it:

```bash
apt install ./ms-a2-fancontrol_0.5.0_all.deb
```

### From source

Building needs `dpkg-deb`, which every Debian system has:

```bash
git clone https://github.com/proxforge/ms-a2-fancontrol.git
cd ms-a2-fancontrol
bash test/run-tests.sh
./build-deb.sh
apt install ./ms-a2-fancontrol_0.5.0_all.deb
```

The package enables and starts the service immediately (`ms-a2-fancontrol.service`). It also starts automatically at every boot.

## Checking that it works

```bash
ms-a2-fancontrol status
```

```text
NVMe Composite 53 C, Sensor 1 67 C, DIMM 38 C, CPU 52 C
System fan levels [32, 36, 36] (firmware [26, 36, 36])
CPU fan levels    [16, 16, 22, 26, 30, 33, 35] (firmware [16, 16, 22, 26, 30, 33, 35])
EC temperatures   cpu 50 C, system 34 C
Fan speed         cpu 1512 rpm, system 3199 rpm
Service           cpu fan floor 16 (wanted 16, set by firmware), system fan floor 32 (wanted 32)
Emergencies       0 since start 2 h 14 min ago
```

- **Fan levels** are target speeds in units of 100 rpm, so level 32 means about 3200 rpm. The first value of each curve is the one the service raises. Values in brackets are the firmware defaults.
- **Sensor 1** is the NVMe controller sensor, which runs hotter than the Composite value.
- **Service** shows the levels the service set and what set the CPU fan: `ram`, `cpu`, `emergency`, or `firmware` (nothing is warm). While the fans slow down, the floor is above the wanted level.
- **Emergencies** counts how often the emergency level was set since the service started. A count that keeps rising means a threshold is too low or a curve too weak; the journal has the details.

Watch what the service does:

```bash
systemctl status ms-a2-fancontrol
journalctl -u ms-a2-fancontrol -f
```

It logs a line whenever it changes a level, for example:

```text
nvme 61/71 C dimm 47 C cpu 86 C -> system fan level 49, cpu fan floor 36 (cpu)
emergency #1: cpu 95 C
emergency over: all sensors at least 5 C below their thresholds
```

## Configuration

Settings live in `/etc/default/ms-a2-fancontrol`. All entries are commented out and show the defaults. Uncomment and change a value, then restart the service:

```bash
systemctl restart ms-a2-fancontrol
```

| Setting | Default | Meaning |
|---|---|---|
| `INTERVAL` | 5 | Seconds between two readings |
| `NVME_LOW` | 50 | NVMe Composite (°C) at which the system fan starts to speed up |
| `NVME_HIGH` | 62 | NVMe Composite (°C) at which the system fan reaches `SYS_LEVEL_MAX` |
| `SYS_LEVEL_MAX` | 50 | Highest regular level of the system fan (about 5000 rpm) |
| `DIMM_LOW` | 50 | RAM temperature (°C) at which the CPU fan starts to speed up |
| `DIMM_HIGH` | 62 | RAM temperature (°C) at which the CPU fan reaches `CPU_LEVEL_MAX` |
| `CPU_LOW` | 80 | CPU Tctl (°C) at which the CPU fan starts to speed up |
| `CPU_HIGH` | 90 | CPU Tctl (°C) at which the CPU fan reaches `CPU_LEVEL_MAX` |
| `CPU_LEVEL_MAX` | 50 | Highest regular minimum level of the CPU fan (about 5000 rpm), for both the RAM and the CPU curve |
| `FALL_STEP` | 1 | Levels the fans slow down per interval. Speeding up is immediate. |
| `NVME_EMERGENCY` | 70 | NVMe Composite (°C) that triggers the emergency level. The drives warn at 77 °C. |
| `NVME_SENSOR1_MAX` | 85 | NVMe controller sensor (°C) that triggers the emergency level |
| `DIMM_EMERGENCY` | 68 | RAM temperature (°C) that triggers the emergency level. DDR5 modules are specified up to 85 °C. |
| `CPU_EMERGENCY` | 95 | CPU Tctl (°C) that triggers the emergency level. The Ryzen 9 7945HX boosts up to its limit of 100 °C, so values around 90 °C are normal under load. |
| `EMERGENCY_LEVEL` | 60 | Emergency level for both fans. 60 is full speed: about 6000 rpm on the CPU fan and 5200 rpm on the system fan. |
| `EMERGENCY_HYST` | 5 | The emergency level ends only when every sensor is this many °C below its threshold. The fans then slow down to the regular curves. |

The service refuses implausible settings and exits with an error in the journal. Examples are `LOW` not below `HIGH`, levels above 60, or an emergency threshold not above `HIGH`.

## How it works

| Fan | Cools | Input | Level |
|---|---|---|---|
| System fan | NVMe | hottest NVMe Composite | Firmware level 26 up to `NVME_LOW`, then linear up to `SYS_LEVEL_MAX` at `NVME_HIGH` |
| CPU fan | CPU and RAM | hottest DDR5 SPD sensor and CPU Tctl | The higher of two ramps: firmware level 16 up to `DIMM_LOW`, then linear up to `CPU_LEVEL_MAX` at `DIMM_HIGH`; and the same from `CPU_LOW` to `CPU_HIGH` for the CPU. The firmware curve (levels 16–35 by CPU temperature) stays the lower limit. |
| both | Emergency | any emergency threshold, or no temperatures readable | `EMERGENCY_LEVEL` (full speed) until every sensor is `EMERGENCY_HYST` below its threshold |

Without the CPU ramp (up to version 0.4.0), the CPU temperature only triggered the emergency level. On a Ryzen 9 7945HX under constant light load this produced a sawtooth: the CPU reached 92 °C, both fans went to full speed, the CPU cooled to about 80 °C within seconds, the fans slowed back to level 16 within about 3.5 minutes, and the cycle repeated every 3 to 7 minutes. The CPU ramp holds the CPU fan at a steady, higher level instead.

- The EC drives each fan to a target speed from its fan curve and adjusts the PWM signal itself. The service only raises the **speed levels of these curves**. It never writes temperature thresholds, PWM registers or EC commands.
- Every write is read back and verified. A mismatch stops the service, and the emergency level applies.
- The fans speed up at once and slow down by one level per interval, so they don't hunt.

What happens when the service stops:

| Event | Fans |
|---|---|
| `systemctl stop`, `apt remove`, shutdown or reboot | Firmware levels are restored. |
| Crash, write error, `kill -9` | **Emergency level (full speed).** systemd restarts the service after 10 seconds. The service then starts from the current levels and slows down gradually, which takes about 3 to 4 minutes from full speed to idle. |
| Boot | The firmware curve applies until the service starts, a few seconds after the kernel modules load. |

So the fans never fall back to the quiet firmware curve unnoticed after a failure.

The service writes its state (levels, what set them, emergency count) to `/run/ms-a2-fancontrol.state` every interval for `ms-a2-fancontrol status`, and removes it on a regular stop.

All EC accesses hold the lock `/run/ms-a2-fancontrol.lock`. Concurrent accesses to the EC ports would otherwise mix up their command sequences. Stop the service before using other tools that access the EC.

## Uninstalling

```bash
apt remove ms-a2-fancontrol
```

This stops the service and restores the firmware fan levels. `apt purge ms-a2-fancontrol` also removes `/etc/default/ms-a2-fancontrol`. After uninstalling, you can turn Secure Boot back on.

To reset the fan levels by hand while the service is stopped:

```bash
ms-a2-fancontrol restore
```

## Troubleshooting

| Message or symptom | Cause and fix |
|---|---|
| `PermissionError: [Errno 1] Operation not permitted: '/dev/port'` | Secure Boot is on (kernel lockdown). Disable Secure Boot in the BIOS. |
| `unsupported system MS-A2 BIOS x.yz` | The BIOS version is not 1.02. The service only runs on firmware whose EC layout is known. It writes nothing, exits with status 3 and is not restarted, so the firmware curve stays active. See [Other BIOS versions and CPU variants](#other-bios-versions-and-cpu-variants). |
| `unexpected EC profile byte at 0x2f` or `EC level 0x.. = .., expected ..` | The BIOS fan mode is not "Auto", or the firmware uses a different layout. Set the fan mode to "Auto". If the message persists, open an issue with the output of `ms-a2-fancontrol dump`. |
| `no NVMe or DIMM temperature found` and fans at full speed | A temperature sensor is missing. The service runs the fans at the emergency level until it can read temperatures again. Check `grep -h . /sys/class/hwmon/hwmon*/name` and load the missing module, e.g. `modprobe spd5118`. |
| Fans loud for a few minutes after a crash | Expected: after a crash or `kill` the service restarts and slows down from full speed one level every 5 seconds. A regular restart or package update starts from the firmware levels. |
| `EC timeout` | Another program is accessing the EC at the same time. Stop it, or stop this service while using it. |

When opening an issue, include:

```bash
ms-a2-fancontrol dump
journalctl -u ms-a2-fancontrol -n 50 --no-pager
```

### Other BIOS versions and CPU variants

The service only writes to firmware whose fan curve layout is known (BIOS 1.02). To add support for another BIOS version or CPU variant, the EC values of that firmware are needed. `ms-a2-fancontrol dump` collects them. It runs on any BIOS version of the MS-A2 and **only reads**: DMI data, CPU model, sensors, the EC RAM (`0x00–0xff`), the fan curve values and the fan speeds. It never writes to the EC.

You don't need to install the package for this. Run it as root. Secure Boot must be off (see [Requirements](#requirements)), and the BIOS fan mode must be "Auto":

```bash
curl -fsSLO https://raw.githubusercontent.com/proxforge/ms-a2-fancontrol/main/ms-a2-fancontrol
python3 ms-a2-fancontrol dump > ms-a2-dump.txt
```

If the package is installed, stop the service first (`systemctl stop ms-a2-fancontrol`), so the fan levels show the firmware values. Then [open an issue](https://github.com/proxforge/ms-a2-fancontrol/issues) with:

- the file `ms-a2-dump.txt`,
- whether you can test a build on that machine.

Support is added only after someone has tested it on that firmware: the maintainers don't have every BIOS version or CPU variant.

## Measurements

Measured on one MS-A2 under Proxmox VE 9 with BIOS fan mode "Auto":

| Test | Result |
|---|---|
| System fan at firmware level 26 | about 2660 rpm, NVMe Composite 58/61 °C |
| NVMe load (fio), stock firmware curve | NVMe Composite 72 °C, controller sensor 93 °C after 7 min (test stopped) |
| NVMe load (fio), 8 min with the service | system fan level 50 (about 4950 rpm), NVMe Composite at most 68 °C, controller sensor 75 °C |
| Full load (fio and stress-ng), 8 min with the service | NVMe Composite at most 65 °C, controller sensor 77 °C, RAM 52 °C, CPU 90 °C |
| Emergency level for 40 s | CPU fan about 6020 rpm, system fan about 5170 rpm; NVMe 56 → 52 °C, RAM 48 → 43 °C |
| Crash (`systemctl kill -s KILL`) | emergency level at once, restart after 10 s, then one level down every 5 s |
| Idle with the service | system fan level 26–38 (about 2600–3800 rpm), depending on NVMe temperature |

## EC register map

For developers. The EC is an ITE IT5571, accessed through the ACPI EC ports `0x62`/`0x66` (read command `0x80`, write command `0x81`). The layout matches the Minisforum UM780 XTX (kernel driver `minisforum-um780xtx`). The MS-A2 firmware does not answer the OEM speed commands `0xb6`/`0xb7`.

| Address | Content (BIOS 1.02, fan mode "Auto") |
|---|---|
| `0x05` | System temperature (°C) |
| `0x09` | CPU temperature (°C) |
| `0x10–0x27` | CPU fan curve, three bytes per point: level, temperature up, temperature down. Levels at `0x13, 0x16, …, 0x25` = 16, 16, 22, 26, 30, 33, 35 |
| `0x2f` | Profile (`0xb1`) |
| `0x30–0x38` | System fan curve, same scheme. Levels at `0x30, 0x33, 0x36` = 26, 36, 36; thresholds 70/100 °C |

Fan speeds are only readable through indirect access to EC memory (I2EC via Super I/O `0x2e`/`0x2f`):
- Chip ID: `0x2000/0x2001` = `55 71`.
- PWM: `0x1804` (CPU fan) and `0x1805` (system fan). The EC overrides direct writes to them.
- Tachometers: `0x181e/0x181f` (CPU fan) and `0x1820/0x1821` (system fan); rpm ≈ 2 156 220 / counter.

## Development

| File | Purpose |
|---|---|
| `ms-a2-fancontrol` | Service and commands `run`, `status`, `restore`, `stop-post`, `dump`, `version` (Python 3, standard library only) |
| `ms-a2-fancontrol.service` | systemd unit. `ExecStopPost` calls `stop-post`, which restores the firmware levels after a regular stop and sets the emergency level after a failure. |
| `ms-a2-fancontrol.default` | Settings, installed as `/etc/default/ms-a2-fancontrol` |
| `build-deb.sh`, `debian/` | Builds `ms-a2-fancontrol_<version>_all.deb` |
| `test/` | Tests without hardware: hwmon and DMI as directories, the EC as a fake. Run with `bash test/run-tests.sh`. |

The version is `VERSION` in `ms-a2-fancontrol` and must match the first entry of `debian/changelog`. CI runs the tests and builds the package on every push. Pushing a tag `v<version>` publishes a release with the `.deb` and `SHA256SUMS`.

Support for another BIOS version needs that firmware's fan curve values. `ms-a2-fancontrol dump` prints the EC RAM and decodes it with the layout above. See [Other BIOS versions and CPU variants](#other-bios-versions-and-cpu-variants).

## License

[GPL-3.0-or-later](LICENSE)
