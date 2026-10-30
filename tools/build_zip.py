"""Gera dist/auto_render_background.zip, pronto para Preferências > Add-ons > Instalar.

    python tools/build_zip.py
"""

import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = "auto_render_background"


def main():
    src = os.path.join(ROOT, PACKAGE)
    dist = os.path.join(ROOT, "dist")
    os.makedirs(dist, exist_ok=True)
    target = os.path.join(dist, PACKAGE + ".zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(os.listdir(src)):
            if name.endswith(".py"):
                zf.write(os.path.join(src, name), "%s/%s" % (PACKAGE, name))
    print(target)


if __name__ == "__main__":
    main()
