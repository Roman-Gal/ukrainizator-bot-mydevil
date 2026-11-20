import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="One-click control panel for the two local Telegram bots"
    )
    parser.add_argument(
        "--start", action="store_true", help="Start automatically when the panel opens"
    )
    parser.add_argument(
        "--headless", action="store_true", help="Run start/stop/status without a window"
    )
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    if args.headless:
        from .manager import Manager, StartError

        manager = Manager(notify=lambda event: print(event.get("detail", ""), flush=True))
        try:
            manager.stop() if args.stop else manager.start() if args.start else manager.status()
        except StartError as error:
            print(str(error), file=sys.stderr)
            return 1
    else:
        from .gui import run

        run(autostart=args.start)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        log = Path(__file__).resolve().parents[1] / "data/control-panel"
        log.mkdir(parents=True, exist_ok=True)
        (log / "panel-startup.log").write_text(
            f"Startup failed: {type(error).__name__}", encoding="utf-8"
        )
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                "Не вдалося відкрити панель. Перевірте встановлення requirements.txt і журнал data/control-panel/panel-startup.log.",
                "Панель ботів",
                16,
            )
        sys.exit(1)
