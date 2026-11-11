#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Builds the Debian package ms-a2-fancontrol_<version>_all.deb from this directory. Runs on a
# Debian system with dpkg-deb (e.g. a Proxmox VE node), not on Windows. The version comes from
# VERSION in ms-a2-fancontrol; the first line of debian/changelog must match it.
#
# Contents:
#   /usr/sbin/ms-a2-fancontrol
#   /usr/lib/systemd/system/ms-a2-fancontrol.service
#   /etc/default/ms-a2-fancontrol                 (conffile)
#   /usr/share/doc/ms-a2-fancontrol/              README.md, copyright, changelog.gz
#   DEBIAN/                                       control, postinst, prerm, postrm, conffiles, md5sums
#
# Usage: build-deb.sh [<target directory>]   (default: current directory)
set -euo pipefail

src=$(cd "$(dirname "$0")" && pwd)
out=$(cd "${1:-.}" && pwd)
die() { echo "build-deb.sh: ERROR: $*" >&2; exit 1; }

command -v dpkg-deb > /dev/null || die "dpkg-deb missing (needs a Debian system)"
version=$(sed -n 's/^VERSION = "\([0-9][0-9A-Za-z.+~-]*\)"$/\1/p' "$src/ms-a2-fancontrol")
[[ -n $version ]] || die "VERSION missing in ms-a2-fancontrol"
changelog_version=$(sed -n '1s/^ms-a2-fancontrol (\([^)]*\)).*/\1/p' "$src/debian/changelog")
[[ $changelog_version == "$version" ]] || die "debian/changelog ($changelog_version) does not match VERSION ($version)"

files=(ms-a2-fancontrol ms-a2-fancontrol.service ms-a2-fancontrol.default README.md debian/control
    debian/postinst debian/prerm debian/postrm debian/conffiles debian/changelog debian/copyright)
for f in "${files[@]}"; do
    # A Windows checkout with CRLF would produce broken scripts in the package
    ! grep -q $'\r' "$src/$f" || die "$f has CRLF line endings"
done

root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
# mktemp creates 0700; in the package these would be the permissions of /
chmod 755 "$root"
install -D -m 755 "$src/ms-a2-fancontrol" "$root/usr/sbin/ms-a2-fancontrol"
install -D -m 644 "$src/ms-a2-fancontrol.service" "$root/usr/lib/systemd/system/ms-a2-fancontrol.service"
install -D -m 644 "$src/ms-a2-fancontrol.default" "$root/etc/default/ms-a2-fancontrol"
doc=$root/usr/share/doc/ms-a2-fancontrol
install -D -m 644 "$src/README.md" "$doc/README.md"
install -m 644 "$src/debian/copyright" "$doc/copyright"
gzip -9n -c "$src/debian/changelog" > "$doc/changelog.gz"
chmod 644 "$doc/changelog.gz"

mkdir -p "$root/DEBIAN"
size=$(du -sk "$root/usr" "$root/etc" | awk '{s += $1} END {print s}')
sed -e "s/@VERSION@/$version/" -e "s/@SIZE@/$size/" "$src/debian/control" > "$root/DEBIAN/control"
install -m 755 "$src/debian/postinst" "$src/debian/prerm" "$src/debian/postrm" "$root/DEBIAN/"
install -m 644 "$src/debian/conffiles" "$root/DEBIAN/"
(cd "$root" && find usr -type f -print0 | sort -z | xargs -0 md5sum) > "$root/DEBIAN/md5sums"

deb=$out/ms-a2-fancontrol_${version}_all.deb
dpkg-deb --root-owner-group -Zxz --build "$root" "$deb" > /dev/null
echo "$deb"
