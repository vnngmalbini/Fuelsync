from PIL import Image
from pathlib import Path

root = Path(__file__).resolve().parents[1]
img_path = root / 'static' / 'images' / 'gas-station.png'
if not img_path.exists():
    raise SystemExit(f'Missing source image: {img_path}')

out_dir = img_path.parent

with Image.open(img_path) as im:
    # Create 32x32 PNG
    png32 = im.copy()
    png32.thumbnail((32, 32), Image.LANCZOS)
    out_png32 = out_dir / 'favicon-32.png'
    png32.save(out_png32, format='PNG')

    # Create 16x16 PNG
    png16 = im.copy()
    png16.thumbnail((16, 16), Image.LANCZOS)
    out_png16 = out_dir / 'favicon-16.png'
    png16.save(out_png16, format='PNG')

    # Create multi-size ICO (16 and 32)
    ico_path = out_dir / 'favicon.ico'
    sizes = [(16,16),(32,32)]
    # Ensure RGBA -> RGB if needed
    try:
        ims = [im.copy().resize(s, Image.LANCZOS) for s in sizes]
        ims[0].save(ico_path, format='ICO', sizes=sizes)
    except Exception as e:
        # Fallback: save single size
        im32 = im.copy()
        im32.thumbnail((32,32), Image.LANCZOS)
        im32.save(ico_path, format='ICO')

print('Favicons created:', out_png32, out_png16, ico_path)
