#!/bin/bash

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# Prefer the project venv so pyinstaller resolves without a global install.
if [[ -x "$ROOT/.venv/bin/pyinstaller" ]]; then
  export PATH="$ROOT/.venv/bin:$PATH"
fi

APP_NAME="PROVISION"
VERSION=$(grep '^version' pyproject.toml | sed -e 's/version = //' -e 's/"//g')

echo "--- Bundling ${APP_NAME}.app with PyInstaller ---"

PYI_ARGS=(--name "$APP_NAME" --windowed --noconfirm)
# Not Python — PyInstaller won't pick this up on its own. Bundled at the
# same relative path chrome_ops_profile.py looks it up at
# (Path(__file__).resolve().parent / "autofill_extension").
PYI_ARGS+=(--add-data "$ROOT/autofill_extension:autofill_extension")
if [[ -f "$ROOT/assets/icon.icns" ]]; then
  PYI_ARGS+=(--icon "$ROOT/assets/icon.icns")
else
  echo "Note: assets/icon.icns missing - building without a custom icon."
fi
PYI_ARGS+=(run.py)

pyinstaller "${PYI_ARGS[@]}"

echo "--- .app bundle created in dist/${APP_NAME}.app ---"

if [[ ! -d "dist/${APP_NAME}.app" ]]; then
  echo "Error: dist/${APP_NAME}.app was not created." >&2
  exit 1
fi

echo "--- Creating ${APP_NAME}-${VERSION}.pkg installer ---"

mkdir -p pkg-resources pkg-root
rm -rf "pkg-root/${APP_NAME}.app"
cp -R "dist/${APP_NAME}.app" "pkg-root/${APP_NAME}.app"

cat > pkg-resources/postinstall << 'EOF'
#!/bin/bash
echo "Checking for Homebrew..."
if ! command -v brew &> /dev/null; then
    echo "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
echo "Installing/updating Bitwarden CLI..."
brew install bitwarden-cli
exit 0
EOF
chmod +x pkg-resources/postinstall

pkgbuild --root "pkg-root" \
         --install-location "/Applications" \
         --scripts "pkg-resources" \
         "dist/${APP_NAME}-${VERSION}.pkg"

echo "--- Build Complete! ---"
echo "App:       dist/${APP_NAME}.app"
echo "Installer: dist/${APP_NAME}-${VERSION}.pkg"
echo "Or open with: open \"dist/${APP_NAME}.app\""
