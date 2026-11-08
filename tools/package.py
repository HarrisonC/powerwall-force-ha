"""Build a small manual-install ZIP from the integration files only."""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
source = root / "custom_components" / "powerwall_force"
version = json.loads((source / "manifest.json").read_text())["version"]
target = root / "dist" / f"powerwall_force-{version}.zip"
target.parent.mkdir(exist_ok=True)
with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
    for file in sorted(source.rglob("*")):
        if file.is_file() and file.suffix in {".py", ".json", ".yaml"}:
            archive.write(file, file.relative_to(root))
    for name in ("README.md", "THIRD_PARTY_NOTICES.md"):
        archive.write(root / name, f"powerwall_force_docs/{name}")
    for name in ("protocol.md", "local-telemetry.md"):
        archive.write(root / "docs" / name, f"powerwall_force_docs/docs/{name}")
print(f"{target} ({target.stat().st_size:,} bytes)")
