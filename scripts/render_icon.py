import os
import subprocess

from PIL import Image


def render_svg_to_png():
    svg_path = 'rust/synapse-gui/icons/direction-b-refined.svg'
    html_temp = os.path.abspath('rust/synapse-gui/icons/render.html')
    png_512 = os.path.abspath('rust/synapse-gui/icons/icon.png')
    
    with open(svg_path, encoding='utf-8') as f:
        svg_content = f.read()
    
    # Remove ambient background rect so squircle has true transparent corners
    svg_clean = svg_content.replace('<rect width="512" height="512" fill="#08090d" />', '')
    
    with open(html_temp, 'w', encoding='utf-8') as f:
        f.write(f'''<!DOCTYPE html>
<html>
<head>
<style>
  html, body {{
    margin: 0;
    padding: 0;
    width: 512px;
    height: 512px;
    background: transparent !important;
    overflow: hidden;
  }}
  svg {{
    display: block;
    width: 512px;
    height: 512px;
  }}
</style>
</head>
<body>
{svg_clean}
</body>
</html>''')

    edge_exe = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
    cmd = [
        edge_exe,
        '--headless',
        '--disable-gpu',
        '--window-size=512,512',
        '--default-background-color=00000000',
        f'--screenshot={png_512}',
        f'file:///{html_temp.replace(os.sep, "/")}'
    ]
    subprocess.run(cmd, check=True)
    if os.path.exists(html_temp):
        os.remove(html_temp)

    # Now open icon.png and verify
    img = Image.open(png_512).convert('RGBA')
    print('Rendered 512x512 icon mode:', img.mode, 'size:', img.size)

    # Generate all Tauri & Web asset resolutions
    icons_dir = 'rust/synapse-gui/icons'
    sizes = {
        '32x32.png': (32, 32),
        '128x128.png': (128, 128),
        '128x128@2x.png': (256, 256),
    }
    for filename, (w, h) in sizes.items():
        resized = img.resize((w, h), Image.Resampling.LANCZOS)
        out_p = os.path.join(icons_dir, filename)
        resized.save(out_p)
        print('Saved', out_p)

    # Save Windows icon.ico
    ico_path = os.path.join(icons_dir, 'icon.ico')
    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(ico_path, sizes=ico_sizes)
    print('Saved Windows icon.ico:', ico_path)

    # Also update web/public icons and launcher icon if present
    for target in ['web/public/icon-512.png', 'web/public/icon-192.png']:
        if os.path.exists(os.path.dirname(target)):
            t_size = (512, 512) if '512' in target else (192, 192)
            img.resize(t_size, Image.Resampling.LANCZOS).save(target)
            print('Updated', target)

if __name__ == '__main__':
    render_svg_to_png()
