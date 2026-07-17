#!/bin/bash

set -e

APP_NAME="DOWNLOWD"
VERSION=$(grep '^version' pyproject.toml | sed -e 's/version = //' -e 's/"//g')

echo "--- Bundling ${APP_NAME}.app with PyInstaller ---"

pyinstaller --name "$APP_NAME" \
            --windowed \
            --noconfirm \
            --icon "assets/icon.icns" \
            run.py

echo "--- .app bundle created in dist/ ---"

echo "--- Creating ${APP_NAME}-${VERSION}.pkg installer ---"

mkdir -p pkg-resources

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

pkgbuild --root "dist/${APP_NAME}.app" \
         --install-location "/Applications/${APP_NAME}.app" \
         --scripts "pkg-resources" \
         "dist/${APP_NAME}-${VERSION}.pkg"

echo "--- Build Complete! ---"
echo "Installer created at: dist/${APP_NAME}-${VERSION}.pkg"