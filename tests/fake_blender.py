"""Executável falso do Blender: aceita os mesmos argumentos que o add-on usa.

    python fake_blender.py -b arquivo.blend [-y|-Y] [-t N] --python-exit-code 1 -P worker.py -- job.json
"""

import os
import runpy
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    argv = sys.argv[1:]
    blend = argv[argv.index("-b") + 1]
    script = argv[argv.index("-P") + 1]
    exit_code = 0
    if "--python-exit-code" in argv:
        exit_code = int(argv[argv.index("--python-exit-code") + 1])

    sys.path.insert(0, HERE)
    import fake_bpy
    sys.modules["bpy"] = fake_bpy
    print("Blender 3.6.22 (simulado)")
    print("Read blend: \"%s\"" % blend)
    fake_bpy.load_blend(blend)

    sys.argv = ["blender"] + argv
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.exit(exit_code)
    print("Blender quit")
    sys.exit(0)


if __name__ == "__main__":
    main()
