#!/bin/sh
set -eu

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <macos|android|linux|windows> [...]" >&2
  exit 2
fi

for requested_profile in "$@"; do
  case "$requested_profile" in
    macos|android|linux|windows) ;;
    *)
      echo "unsupported native validation profile: $requested_profile" >&2
      exit 2
      ;;
  esac
done

plugin_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
temporary_base=${TMPDIR:-/tmp}
if [ "$temporary_base" != "/" ]; then
  temporary_base=${temporary_base%/}
fi
validation_root=$(mktemp -d "$temporary_base/app-icon-toolkit-native.XXXXXX")

cleanup() {
  case "$validation_root" in
    "$temporary_base"/app-icon-toolkit-native.*)
      rm -rf -- "$validation_root"
      ;;
    *)
      echo "refusing to remove unexpected temporary path: $validation_root" >&2
      return 1
      ;;
  esac
}
trap cleanup EXIT HUP INT TERM

workspace_root=$validation_root/workspace
cargo run --quiet --locked \
  --manifest-path "$plugin_root/Cargo.toml" \
  --package app-icon-engine \
  --example validation_fixture \
  -- "$workspace_root"
generated_root=$workspace_root/generated

run_without_warnings() {
  validator_log=$1
  shift
  if ! "$@" >"$validator_log" 2>&1; then
    cat "$validator_log" >&2
    return 1
  fi
  cat "$validator_log"
  if grep -Eiq '(^|[[:space:]])warning([:[:space:]])' "$validator_log"; then
    echo "validator emitted a warning" >&2
    return 1
  fi
}

validate_macos() {
  command -v xcrun >/dev/null 2>&1 || {
    echo "xcrun is required for the macOS native validation profile" >&2
    return 1
  }
  xcrun --find actool >/dev/null

  asset_catalog=$validation_root/ActoolSmoke.xcassets
  actool_output=$validation_root/actool-output
  mkdir -p "$asset_catalog" "$actool_output"
  cp -R "$generated_root/macos/Assets.appiconset" \
    "$asset_catalog/Assets.appiconset"
  run_without_warnings "$validation_root/actool.log" xcrun actool \
    --compile "$actool_output" \
    --platform macosx \
    --minimum-deployment-target 13.0 \
    --app-icon Assets \
    --output-partial-info-plist "$validation_root/actool-partial-info.plist" \
    --warnings --errors \
    "$asset_catalog"
  test -s "$actool_output/Assets.car"
  test -s "$actool_output/Assets.icns"
  test -s "$validation_root/actool-partial-info.plist"
}

latest_file() {
  search_root=$1
  filename=$2
  find "$search_root" -type f -name "$filename" -print | LC_ALL=C sort -V | tail -n 1
}

validate_android() {
  android_sdk=${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}
  if [ -z "$android_sdk" ] || [ ! -d "$android_sdk" ]; then
    echo "ANDROID_SDK_ROOT or ANDROID_HOME must name an Android SDK" >&2
    return 1
  fi

  if [ -n "${ANDROID_BUILD_TOOLS_VERSION:-}" ]; then
    aapt2=$android_sdk/build-tools/$ANDROID_BUILD_TOOLS_VERSION/aapt2
  else
    aapt2=$(latest_file "$android_sdk/build-tools" aapt2)
  fi
  if [ -n "${ANDROID_PLATFORM:-}" ]; then
    android_jar=$android_sdk/platforms/$ANDROID_PLATFORM/android.jar
  else
    android_jar=$(latest_file "$android_sdk/platforms" android.jar)
  fi
  if [ -z "$aapt2" ] || [ ! -x "$aapt2" ]; then
    echo "no executable aapt2 was found under $android_sdk/build-tools" >&2
    return 1
  fi
  if [ -z "$android_jar" ] || [ ! -f "$android_jar" ]; then
    echo "no android.jar was found under $android_sdk/platforms" >&2
    return 1
  fi

  target_sdk=$(basename "$(dirname "$android_jar")")
  target_sdk=${target_sdk#android-}
  compiled=$validation_root/aapt2-compiled
  apk=$validation_root/icon-smoke.apk
  dump=$validation_root/aapt2-resources.txt
  mkdir -p "$compiled"
  run_without_warnings "$validation_root/aapt2-compile.log" \
    "$aapt2" compile --dir "$generated_root/android/res" -o "$compiled"
  run_without_warnings "$validation_root/aapt2-link.log" "$aapt2" link \
    -o "$apk" \
    -I "$android_jar" \
    --manifest "$plugin_root/crates/app-icon-engine/tests/fixtures/AndroidManifest.xml" \
    --min-sdk-version 26 \
    --target-sdk-version "$target_sdk" \
    "$compiled"/*.flat
  if ! "$aapt2" dump resources "$apk" >"$dump" 2>"$validation_root/aapt2-dump.log"; then
    cat "$validation_root/aapt2-dump.log" >&2
    return 1
  fi
  cat "$validation_root/aapt2-dump.log"
  if grep -Eiq '(^|[[:space:]])warning([:[:space:]])' "$validation_root/aapt2-dump.log"; then
    echo "AAPT2 dump emitted a warning" >&2
    return 1
  fi
  test -s "$apk"
  for resource_name in \
    mipmap/ic_launcher \
    mipmap/ic_launcher_background \
    mipmap/ic_launcher_foreground \
    mipmap/ic_launcher_monochrome
  do
    grep -q "$resource_name" "$dump"
  done
}

validate_linux() {
  command -v desktop-file-validate >/dev/null 2>&1 || {
    echo "desktop-file-validate is required for the Linux native validation profile" >&2
    return 1
  }
  run_without_warnings "$validation_root/desktop-file-validate.log" \
    desktop-file-validate \
    "$generated_root/linux/share/applications/com.example.IconProbe.desktop"
}

validate_windows() {
  command -v icotool >/dev/null 2>&1 || {
    echo "icotool is required for the Windows ICO validation profile" >&2
    return 1
  }
  ico_listing=$validation_root/ico-listing.txt
  ico_output=$validation_root/ico-frames
  mkdir -p "$ico_output"
  if ! icotool --list "$generated_root/windows/icon-probe.ico" \
    >"$ico_listing" 2>"$validation_root/icotool-list.log"
  then
    cat "$validation_root/icotool-list.log" >&2
    return 1
  fi
  cat "$validation_root/icotool-list.log"
  if grep -Eiq '(^|[[:space:]])warning([:[:space:]])' "$validation_root/icotool-list.log"; then
    echo "icotool list emitted a warning" >&2
    return 1
  fi
  run_without_warnings "$validation_root/icotool-extract.log" \
    icotool --extract --output="$ico_output" \
    "$generated_root/windows/icon-probe.ico"
  test "$(find "$ico_output" -type f -name '*.png' | wc -l | tr -d ' ')" -eq 5
  for width in 16 24 32 48 256; do
    grep -q -- "--width=$width" "$ico_listing"
  done
}

for requested_profile in "$@"; do
  case "$requested_profile" in
    macos) validate_macos ;;
    android) validate_android ;;
    linux) validate_linux ;;
    windows) validate_windows ;;
  esac
done
