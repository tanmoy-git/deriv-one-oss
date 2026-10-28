"""Write the API's OpenAPI document to stdout or a file.

Used to generate the frontend's typed schema (src/api/schema.d.ts):

    npm run api:types

The document is built from main.app with the same placeholder environment the
tests use, so the output depends only on the code, never on local settings.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

for key, value in {
    "ENABLE_MOCK_AUTH": "true",
    "APP_ENV": "development",
    "MODEL_GATEWAY_URL": "http://127.0.0.1:4000",
}.items():
    os.environ.setdefault(key, value)

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    import logging

    logging.disable(logging.WARNING)
    from main import app

    doc = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(doc)
    else:
        sys.stdout.write(doc)


if __name__ == "__main__":
    main()
