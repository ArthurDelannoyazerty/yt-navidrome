"""Export the actual backend contracts, with no optional runtime dependencies."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from state_machines import MACHINES


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--format', choices=('json', 'mermaid'), default='json')
    parser.add_argument('--machine', choices=sorted(MACHINES), help='Export one contract')
    args = parser.parse_args()
    machines = [MACHINES[args.machine]] if args.machine else list(MACHINES.values())
    if args.format == 'json':
        print(json.dumps([spec.describe() for spec in machines], indent=2))
        return
    for spec in machines:
        print(f'## {spec.name}\n\n```mermaid\nstateDiagram-v2\n    [*] --> {spec.initial}')
        for state, events in spec.transitions.items():
            for event, target in events.items():
                print(f'    {state} --> {target}: {event}')
            if not events:
                print(f'    {state} --> [*]')
        print('```\n')


if __name__ == '__main__':
    main()
