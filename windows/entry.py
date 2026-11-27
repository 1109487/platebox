"""Entry point of the Windows program (what PyInstaller freezes into platebox.exe)."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()          # a frozen program that spawns workers must say so, or it re-launches itself
    from platebox.launcher import main

    raise SystemExit(main())
