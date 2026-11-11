#!/bin/bash
# Tests for ms-a2-fancontrol without hardware (hwmon and DMI as directories, EC as a fake).
# Usage: bash test/run-tests.sh
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
exec python3 "$here/test_fancontrol.py" -v
