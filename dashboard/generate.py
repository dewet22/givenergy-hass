#!/usr/bin/env python3
"""
Generate a GivEnergy Local dashboard with serial numbers pre-filled.

The Battery view uses HA's sections layout — one column per battery — so pass
as many --battery serials as you have packs and the columns are built for you.

Usage:
    uv run dashboard/generate.py \\
        --inverter sa1234g123 \\
        --battery bt1234a001 bt1234a002

    # write to a file:
    uv run dashboard/generate.py --inverter sa1234g123 --battery bt1234a001 \\
        --output ~/homeassistant/dashboard_givenergy.yaml

    # pipe to clipboard (macOS):
    uv run dashboard/generate.py --inverter sa1234g123 --battery bt1234a001 | pbcopy
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

# Load dashboard.py directly to avoid triggering the HA component's __init__.py.
_spec = importlib.util.spec_from_file_location(
    "givenergy_local_dashboard",
    Path(__file__).parent.parent / "custom_components" / "givenergy_local" / "dashboard.py",
)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]
generate_dashboard = _mod.generate_dashboard


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--inverter", required=True, metavar="SERIAL",
                   help="Inverter serial number (lowercase, e.g. sa1234g123)")
    p.add_argument("--battery", required=True, nargs="+", metavar="SERIAL",
                   help="Battery serial number(s) (lowercase), space-separated")
    p.add_argument("--output", "-o", metavar="FILE",
                   help="Write to FILE instead of stdout")
    args = p.parse_args()

    dashboard = generate_dashboard(args.inverter, args.battery)

    if args.output:
        Path(args.output).write_text(dashboard)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(dashboard)


if __name__ == "__main__":
    main()
