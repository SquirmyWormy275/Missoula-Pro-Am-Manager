"""Entry point for the synthetic lab; no import-time app/database side effects."""
import sys
from pathlib import Path

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from recovery_drills.cli import main
    raise SystemExit(main())
