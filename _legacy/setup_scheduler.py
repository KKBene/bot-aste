#!/usr/bin/env python3
"""
Configura il bot per girare AUTOMATICAMENTE ogni venerdì mattina alle 08:00.

Usa macOS LaunchAgent (più affidabile di cron, sopravvive allo sleep).

Esegui UNA VOLTA:  python3 setup_scheduler.py [--install | --uninstall | --status]
"""
import os
import sys
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PYTHON_PATH = sys.executable
MAIN_SCRIPT = str(SCRIPT_DIR / "main.py")
LOG_DIR = SCRIPT_DIR / "logs"
PLIST_LABEL = "com.user.aste_bot"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


PLIST_CONTENT = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{PYTHON_PATH}</string>
        <string>{MAIN_SCRIPT}</string>
    </array>

    <!-- Ogni venerdì (Weekday=5) alle 08:00 -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>5</integer>
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <key>WorkingDirectory</key>
    <string>{SCRIPT_DIR}</string>

    <key>StandardOutPath</key>
    <string>{LOG_DIR}/bot.log</string>

    <key>StandardErrorPath</key>
    <string>{LOG_DIR}/bot_error.log</string>

    <!-- Riavvia se crashа -->
    <key>KeepAlive</key>
    <false/>

    <!-- Aspetta connessione rete prima di avviare -->
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""


def install():
    LOG_DIR.mkdir(exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    PLIST_PATH.write_text(PLIST_CONTENT)
    print(f"✅ plist scritto: {PLIST_PATH}")

    # Unload se già caricato
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
    # Load
    result = subprocess.run(["launchctl", "load", str(PLIST_PATH)], capture_output=True, text=True)

    if result.returncode == 0:
        print("✅ LaunchAgent installato e attivato")
        print(f"   Script:  {MAIN_SCRIPT}")
        print(f"   Python:  {PYTHON_PATH}")
        print(f"   Orario:  Ogni venerdì alle 08:00")
        print(f"   Log:     {LOG_DIR}/bot.log")
    else:
        print(f"❌ Errore launchctl load: {result.stderr}")


def uninstall():
    if not PLIST_PATH.exists():
        print("ℹ️  LaunchAgent non installato")
        return

    subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
    PLIST_PATH.unlink()
    print(f"✅ LaunchAgent rimosso: {PLIST_PATH}")


def status():
    result = subprocess.run(
        ["launchctl", "list", PLIST_LABEL],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"✅ LaunchAgent attivo:\n{result.stdout}")
    else:
        print("❌ LaunchAgent non attivo o non installato")

    if PLIST_PATH.exists():
        print(f"   plist: {PLIST_PATH} ✅")
    else:
        print(f"   plist: non trovato ❌")

    log = LOG_DIR / "bot.log"
    if log.exists():
        size = log.stat().st_size
        print(f"   Log:   {log} ({size/1024:.1f} KB)")
        # Ultime 5 righe del log
        lines = log.read_text().splitlines()[-5:]
        if lines:
            print("   Ultimi log:")
            for l in lines:
                print(f"     {l}")


def test_now():
    """Esegue il bot subito (per testare)."""
    print(f"🚀 Avvio test: {PYTHON_PATH} {MAIN_SCRIPT}")
    os.execv(PYTHON_PATH, [PYTHON_PATH, MAIN_SCRIPT] + sys.argv[2:])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "--help"

    if cmd == "--install":
        install()
    elif cmd == "--uninstall":
        uninstall()
    elif cmd == "--status":
        status()
    elif cmd == "--run-now":
        test_now()
    else:
        print(__doc__)
        print("\nComandi disponibili:")
        print("  python3 setup_scheduler.py --install    # Installa cron venerdì 08:00")
        print("  python3 setup_scheduler.py --uninstall  # Rimuovi")
        print("  python3 setup_scheduler.py --status     # Controlla stato")
        print("  python3 setup_scheduler.py --run-now    # Esegui subito (test)")
