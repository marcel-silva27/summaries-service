"""Genera `docs/eventos/<tipo>.schema.json` desde los modelos de `app/event_schemas.py`.

    python -m app.tools.export_event_schemas [DIRECTORIO]
"""
import json
import sys
from pathlib import Path

from ..event_schemas import EVENTS, json_schema


def render(event_type: str) -> str:
    return json.dumps(json_schema(event_type), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    target = Path(argv[0]) if argv else Path("docs/eventos")
    target.mkdir(parents=True, exist_ok=True)
    for event_type in EVENTS:
        (target / f"{event_type}.schema.json").write_text(render(event_type), encoding="utf-8")
    print(f"{len(EVENTS)} esquemas en {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
