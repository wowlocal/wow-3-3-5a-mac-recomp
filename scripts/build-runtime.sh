#!/bin/zsh
set -eu

if (( $# != 2 )); then
    print -u2 'Usage: build-runtime.sh AARCHX_CHECKOUT OUTPUT_APP'
    exit 2
fi

repo_dir="${0:A:h:h}"
checkout="${1:A}"
app="${2:A}"
revision=96754ec605d2cf57e2a05d0cf0f6d2e1c8177741
patch="$repo_dir/patches/aarchx-wrath.patch"
if [[ "$(git -C "$checkout" rev-parse HEAD)" != "$revision" ]]; then
    print -u2 "Check out AArchX revision $revision before building."
    exit 1
fi
if [[ -e "$app" ]]; then
    print -u2 "Choose a new output app path or move the existing bundle aside."
    exit 1
fi
if git -C "$checkout" apply --check "$patch" 2>/dev/null; then
    git -C "$checkout" apply "$patch"
elif ! git -C "$checkout" apply --reverse --check "$patch" 2>/dev/null; then
    print -u2 'The patch does not match this checkout. Use a fresh checkout of the pinned revision.'
    exit 1
fi

jobs="$(/usr/sbin/sysctl -n hw.ncpu)"
make -C "$checkout" -j "$jobs" ocerz
mkdir -p "${app:h}"
# Sign the bundle in a sibling directory so an interrupted build leaves no partial app.
staging="$(/usr/bin/mktemp -d "${app:h}/.wrath-build.XXXXXX")"
staged_app="$staging/${app:t}"
mkdir -p "$staged_app/Contents/MacOS"
install -m 755 "$checkout/ocerz" "$staged_app/Contents/MacOS/ocerz"
install -m 644 "$repo_dir/support/Info.plist" "$staged_app/Contents/Info.plist"
/usr/bin/codesign --force --sign - "$staged_app"
/usr/bin/codesign --verify --strict "$staged_app"
mv -n "$staged_app" "$app"
if [[ -e "$staged_app" ]]; then
    print -u2 "The destination appeared during the build. Signed bundle retained at $staged_app"
    exit 1
fi
rmdir "$staging"
print "Built $app"
