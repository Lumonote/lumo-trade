#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 ]]; then
    echo "Usage: $0 <signed .app bundle> <output directory>" >&2
    exit 2
fi

app_path="${1%/}"
output_dir="$2"
app_file="${app_path##*/}"
plist="$app_path/Contents/Info.plist"
product="${app_file%.app}"

if [[ "$app_file" != *.app || ! -f "$plist" ]]; then
    echo "Missing macOS app bundle: $app_path" >&2
    exit 1
fi

version=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$plist")
executable=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$plist")
arch=$(/usr/bin/lipo -archs "$app_path/Contents/MacOS/$executable")
case "$arch" in
    x86_64) suffix=x64 ;;
    arm64) suffix=aarch64 ;;
    *) echo "Unsupported macOS bundle architecture: $arch" >&2; exit 1 ;;
esac

mkdir -p "$output_dir"
work_dir=$(mktemp -d "$output_dir/.lumo-dmg.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT
contents="$work_dir/contents"
mkdir "$contents"
/usr/bin/ditto "$app_path" "$contents/$app_file"
/usr/bin/codesign --verify --strict "$contents/$app_file"
ln -s /Applications "$contents/Applications"
: > "$contents/.metadata_never_index"

dmg_name="${product}_${version}_${suffix}.dmg"
for attempt in 1 2 3; do
    if /usr/bin/hdiutil create -ov -volname "$product" -srcfolder "$contents" \
        -format UDZO -fs HFS+ -nospotlight "$work_dir/$dmg_name"; then
        /usr/bin/hdiutil verify "$work_dir/$dmg_name"
        mv -f "$work_dir/$dmg_name" "$output_dir/$dmg_name"
        printf 'Created %s\n' "$output_dir/$dmg_name"
        exit 0
    fi
    if [[ "$attempt" != 3 ]]; then
        echo "Disk image creation failed (attempt $attempt/3), retrying." >&2
        sleep 5
    fi
done

echo "Disk image creation failed after three attempts." >&2
exit 1
