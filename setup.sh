#!/bin/bash

#
# DOWNLOWD - Automated Setup Script for macOS
#
# This script installs all necessary system and Python dependencies
# to run the DOWNLOWD application.
#

set -e # Exit immediately if a command exits with a non-zero status.

echo "--- Starting DOWNLOWD Setup ---"

# 1. Check for and install Homebrew
if ! command -v brew &> /dev/null; then
    echo "Homebrew not found. Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
else
    echo "Homebrew is already installed."
fi

# 2. Install system prerequisites using Homebrew
echo "Installing system prerequisites (Python with Tkinter, Bitwarden CLI, ChromeDriver)..."
brew install python-tk
brew install bitwarden-cli
brew install --cask chromedriver

# 3. Create a Python virtual environment
echo "Creating Python virtual environment in ./.venv..."
python3 -m venv .venv

# 4. Install Python dependencies into the virtual environment
echo "Installing Python dependencies (requests, msal, selenium)..."
.venv/bin/pip install -e .

echo ""
echo "--- Setup Complete! ---"
echo ""
echo "To run the application, follow these steps:"
echo "1. Activate the virtual environment: source .venv/bin/activate"
echo "2. Unlock your Bitwarden vault:   bw unlock"
echo "3. Run the application:             python3 run.py"
echo ""