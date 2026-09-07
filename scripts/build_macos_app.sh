#!/bin/bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
app_bundle="$project_root/Amazon竞品监控.app"
source_icon="$project_root/assets/app-icon.png"
icon_output="$app_bundle/Contents/Resources/AppIcon.icns"
launcher_output="$app_bundle/Contents/MacOS/launcher"
temp_root="$(mktemp -d /tmp/amazon-monitor-app.XXXXXX)"
iconset_dir="$temp_root/AppIcon.iconset"
trap '/bin/rm -rf -- "$temp_root"' EXIT

mkdir -p "$iconset_dir" "$app_bundle/Contents/MacOS" "$app_bundle/Contents/Resources"

sips -z 16 16 "$source_icon" --out "$iconset_dir/icon_16x16.png" >/dev/null
sips -z 32 32 "$source_icon" --out "$iconset_dir/icon_16x16@2x.png" >/dev/null
sips -z 32 32 "$source_icon" --out "$iconset_dir/icon_32x32.png" >/dev/null
sips -z 64 64 "$source_icon" --out "$iconset_dir/icon_32x32@2x.png" >/dev/null
sips -z 128 128 "$source_icon" --out "$iconset_dir/icon_128x128.png" >/dev/null
sips -z 256 256 "$source_icon" --out "$iconset_dir/icon_128x128@2x.png" >/dev/null
sips -z 256 256 "$source_icon" --out "$iconset_dir/icon_256x256.png" >/dev/null
sips -z 512 512 "$source_icon" --out "$iconset_dir/icon_256x256@2x.png" >/dev/null
sips -z 512 512 "$source_icon" --out "$iconset_dir/icon_512x512.png" >/dev/null
sips -z 1024 1024 "$source_icon" --out "$iconset_dir/icon_512x512@2x.png" >/dev/null
iconutil -c icns "$iconset_dir" -o "$icon_output"

xcrun clang -fobjc-arc -mmacosx-version-min=10.13 \
  -arch x86_64 -arch arm64 \
  -framework Foundation -framework AppKit \
  "$project_root/macos/AppLauncher.m" -o "$launcher_output"

codesign --force --deep --sign - "$app_bundle"
touch "$app_bundle"
printf '已生成：%s\n' "$app_bundle"
