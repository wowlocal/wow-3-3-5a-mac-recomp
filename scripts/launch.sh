#!/bin/zsh
set -eu

if (( $# < 4 )); then
    print -u2 'Usage: launch.sh TRIAL_APP AARCHX_RUNTIME WRITABLE_GAME_DIR LOCALE [--profile] [--trace-settings] [--check]'
    exit 2
fi

app="${1:A}"
runtime="${2:A}"
game="${3:A}"
locale="$4"
shift 4
profile=0
trace_settings=0
check_only=0
for option in "$@"; do
    case "$option" in
        --profile) profile=1 ;;
        --trace-settings) trace_settings=1 ;;
        --check) check_only=1 ;;
        *) print -u2 "Unknown option $option"; exit 2 ;;
    esac
done

executable="$app/Contents/MacOS/ocerz"
if [[ ! -x "$executable" || ! -d "$runtime/apis" || ! -d "$runtime/apis32" || ! -d "$runtime/guest32" ]]; then
    print -u2 'Build the trial app and provide the extracted AArchX runtime directory with guest32 installed.'
    exit 1
fi
if [[ ! -d "$game/World of Warcraft.app" || ! -d "$game/WTF" || ! -w "$game/WTF" ]]; then
    print -u2 'Use the writable Mac game folder containing World of Warcraft.app, Data, and WTF.'
    exit 1
fi
if [[ ! "$locale" =~ '^[a-z]{2}[A-Z]{2}$' ]]; then
    print -u2 'Supply the four-letter game locale, such as enUS or ruRU.'
    exit 2
fi
config="$game/WTF/Config.wtf"
if [[ -f "$config" ]]; then
    configured_locale=$(/usr/bin/awk 'tolower($1) == "set" && tolower($2) == "locale" {
        gsub(/["\r]/, "", $3); locale = $3
    } END { print locale }' "$config")
    if [[ -n "$configured_locale" && "$configured_locale" != "$locale" ]]; then
        print -u2 "Pass the locale selected in WTF/Config.wtf ($configured_locale)."
        exit 1
    fi
fi
if ! /usr/bin/awk 'tolower($1) == "set" && tolower($2) ~ /^(realmlist|patchlist)$/ {
    gsub(/["\r]/, "", $3)
    seen[tolower($2)] = 1
    if ($3 != "127.0.0.1") bad = 1
} END { exit !(seen["realmlist"] && seen["patchlist"] && !bad) }' "$game/Data/$locale/realmlist.wtf"; then
    print -u2 "Set both realmlist and patchlist in Data/$locale/realmlist.wtf to 127.0.0.1."
    exit 1
fi
if (( check_only )); then
    print "Verified the local trial paths and the $locale loopback realm setting."
    exit 0
fi

if [[ "${WRATH_TRIAL_LOCKED:-}" != "$app" ]]; then
    typeset -a flags
    flags=()
    if (( profile )); then flags+=(--profile); fi
    if (( trace_settings )); then flags+=(--trace-settings); fi
    exec /usr/bin/lockf -k -t 0 "${app:h}/.wrath-launch.lock" \
        /usr/bin/env "WRATH_TRIAL_LOCKED=$app" "$0" "$app" "$runtime" "$game" "$locale" "${flags[@]}"
fi

trial_pid() {
    local pid command_path
    while read -r pid command_path; do
        if [[ "$command_path" == "$executable" ]]; then
            print "$pid"
            return 0
        fi
    done < <(/bin/ps -ax -o pid=,comm=)
    return 1
}
if running=$(trial_pid); then
    print "Wrath Trial is already running (PID $running). Use Exit in the game before relaunching."
    exit 0
fi

log_dir="${WRATH_TRIAL_LOG_DIR:-${app:h}/Logs}"
mkdir -p "$log_dir"
log_dir="${log_dir:A}"
log="$log_dir/wrath-$(date +%Y%m%d-%H%M%S)-$$"
typeset -a extra
extra=()
if (( profile )); then
    extra+=(--env "OCERZ_M32_FRAME_TIMES=$log.frames.ns")
    print "Frame log: $log.frames.ns"
fi
if (( trace_settings )); then
    extra+=(--env OCERZ_M32LOG=files,exit --env OCERZ_M32_SETTINGS_IO=1)
fi

/usr/bin/open -n --stdout "$log.stdout.log" --stderr "$log.log" \
    "${extra[@]}" \
    --env "OCERZ_PROBE_WORKDIR=$game" \
    --env "OCERZ_APIDB=$runtime/apis" --env "OCERZ_APIDB32=$runtime/apis32" \
    --env "OCERZ_GUEST32_ROOT=$runtime/guest32" \
    --env OCERZ_M32_ACTIVATE=1 --env OCERZ_PROBE_AGL_CONTENT_CLICKS=1 \
    --env OCERZ_DIAG_ALERT=1 --env OCERZ_DIAG_QUICKTIME_VERSION=1 \
    --env OCERZ_NO_M32_FAULT_HANDLERS=1 --env OCERZ_DIAG_KEEP_HOST_EXCEPTIONS=1 \
    --env OCERZ_ABORT_REPORT=1 --env OCERZ_NO_XLIVE=1 \
    --env OCERZ_TCACHE=off --env OCERZ_NO_CACHE_HOLDER=1 \
    "$app" --args -native "$game/World of Warcraft.app" -nosound

for attempt in {1..25}; do
    if running=$(trial_pid); then
        print "Wrath Trial is running (PID $running)."
        exit 0
    fi
    /bin/sleep 0.2
done
print -u2 "The trial did not stay up. Check $log.log."
exit 1
