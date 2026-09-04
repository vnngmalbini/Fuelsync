from pathlib import Path
import shutil
import sys

root = Path(__file__).resolve().parent.parent
src = root / 'static' / 'images' / 'favicon.ico'
dst = root / 'favicon.ico'

if not src.exists():
    print(f"Source not found: {src}")
    sys.exit(2)

shutil.copy2(src, dst)
print(f"Copied {src} -> {dst}")
