# DOWNLOWD - Employee Onboarding Appliance

An automated desktop tool for streamlining new employee onboarding tasks. The application monitors the Downloads folder for specific employee data files, converts them for Bitwarden import, and automates the creation of M365, Hyatt, and Marriott accounts.

## Features

- **File Monitoring**: Automatically detects new employee data files (`HQ-*.txt`, `HQ-*.rtf`) in the Downloads folder.
- **Bitwarden Integration**: Converts employee data into Bitwarden-compatible JSON and imports it into a specified collection.
- **Account Provisioning**:
  - Creates Microsoft 365 user mailboxes via Graph API.
  - Creates Hyatt and Marriott loyalty accounts via browser automation.
- **Secure File Handling**: Securely deletes local data files after processing.
- **Interactive GUI**: A user-friendly interface to control and monitor the onboarding process, with toggles for each task.

---

## Installation and Setup

### Automated Setup (macOS)

For macOS users, the easiest way to install all dependencies is to run the provided setup script. This will install Homebrew (if not present), system tools, and all required Python packages.

1.  **Make the script executable**:
    ```bash
    chmod +x setup.sh
    ```
2.  **Run the script**:
    ```bash
    ./setup.sh
    ```

After the script finishes, follow the final instructions printed in your terminal to run the application.

### Manual Setup

Follow these steps to set up the development environment for the application.

### 1. Prerequisites

Before you begin, ensure you have the following installed on your system:

- **Python 3.8+ with Tkinter**:
  - **macOS**: The recommended way to install Python is with Homebrew. This ensures `tkinter` is included.
    ```bash
    brew install python-tk
    ```
  - **Windows/Linux**: Standard Python installers typically include Tkinter.

- **Bitwarden CLI**: The application requires the `bw` command-line tool. Follow the official installation guide.

- **ChromeDriver**: The browser automation for partner accounts requires `chromedriver`. Ensure it is installed and accessible in your system's `PATH`.

### 2. Setup Instructions

1.  **Clone the Repository**:
    ```bash
    git clone https://github.com/CMRNCHN/DOWNLOWd.git
    cd DOWNLOWd
    ```

2.  **Create a Virtual Environment**:
    This creates an isolated environment for the project's dependencies.
    ```bash
    python3 -m venv .venv
    ```

3.  **Activate the Virtual Environment**:
    You must activate the environment in your terminal session before installing packages or running the app.
    ```bash
    # On macOS or Linux
    source .venv/bin/activate

    # On Windows
    # .venv\Scripts\activate
    ```

4.  **Install Dependencies**:
    This command reads the `pyproject.toml` file and installs `selenium`, `msal`, `requests`, and other required packages.
    ```bash
    pip install -e .
    ```

---

## How to Run

1.  **Unlock Bitwarden**: Before launching the app, ensure your Bitwarden vault is unlocked.
    ```bash
    bw unlock
    ```

2.  **Run the Application**: With your virtual environment activated, start the GUI.
    ```bash
    python3 run.py
    ```

3.  **Configure and Use**:
    - Use the **M365 Settings** button to enter your Microsoft Graph API credentials.
    - Enter a common **Initial Password** for the new accounts that will be created.
    - The app will now monitor your Downloads folder. You can also trigger a run manually with the **Run Import Now** button.

---

## Creating a Distributable Application

To create a single, standalone executable file that can be run on other machines without needing to install Python or any dependencies, you can use PyInstaller.

1.  **Install PyInstaller**:
    ```bash
    pip install pyinstaller
    ```

2.  **Build the Application**:
    Run the following command from the project root directory.
    ```bash
    pyinstaller --name="DOWNLOWD" --onefile --windowed run.py
    ```
    - `--name`: Sets the name of the final application.
    - `--onefile`: Bundles everything into a single executable file.
    - `--windowed`: Prevents a terminal window from opening when the GUI is launched.

3.  **Find the Executable**:
    The final application will be located in the `dist` folder (e.g., `dist/DOWNLOWD`). You can copy this file to another machine and run it directly.