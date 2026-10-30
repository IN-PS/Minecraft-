"""bpy simulado para executar worker.py fora do Blender.

O "arquivo .blend" usado nos testes é um JSON que descreve a cena e o
comportamento desejado da renderização (normal, travar, falhar, lento...).
"""

import json
import os
import re
import sys
import time

_BEHAVIOR = {}


class _Handlers:
    def __init__(self):
        self.render_pre = []
        self.render_post = []
        self.render_write = []
        self.render_cancel = []


class _App:
    def __init__(self):
        self.handlers = _Handlers()
        self.binary_path = sys.executable


class _ImageSettings:
    file_format = "PNG"


class _Render:
    def __init__(self, engine):
        self.engine = engine
        self.filepath = "/tmp/"
        self.use_overwrite = True
        self.resolution_x = 1920
        self.resolution_y = 1080
        self.resolution_percentage = 100
        self.image_settings = _ImageSettings()

    def frame_path(self, frame=0):
        path = self.filepath
        match = re.search(r"#+", os.path.basename(path))
        if match:
            hashes = match.group(0)
            return path[: len(path) - len(os.path.basename(path))] + \
                os.path.basename(path).replace(hashes, str(frame).zfill(len(hashes)), 1) + ".png"
        return path + str(frame).zfill(4) + ".png"


class _Cycles:
    def __init__(self, device):
        self.device = device


class _Scene:
    def __init__(self, name, data):
        self.name = name
        self.frame_start = data.get("frame_start", 1)
        self.frame_end = data.get("frame_end", 3)
        self.frame_step = data.get("frame_step", 1)
        self.frame_current = self.frame_start
        self.render = _Render(data.get("engine", "CYCLES"))
        self.cycles = _Cycles(data.get("device", "CPU"))


class _Scenes(dict):
    def get(self, name, default=None):
        return dict.get(self, name, default)


class _Data:
    def __init__(self):
        self.scenes = _Scenes()
        self.filepath = ""


class _Device:
    def __init__(self, name, type_, use):
        self.name, self.type, self.use = name, type_, use


class _CyclesPrefs:
    def __init__(self, gpu):
        self.compute_device_type = "CUDA" if gpu else "NONE"
        self.devices = [_Device("CPU", "CPU", True)]
        if gpu:
            self.devices.append(_Device("Fake GPU", "CUDA", True))
        self.refreshed = False

    def refresh_devices(self):
        self.refreshed = True


class _Addon:
    def __init__(self, prefs):
        self.preferences = prefs


class _Preferences:
    def __init__(self, gpu):
        self.addons = {"cycles": _Addon(_CyclesPrefs(gpu))}


class _Context:
    def __init__(self, gpu):
        self.window = None
        self.preferences = _Preferences(gpu)


def _call(handlers, scene):
    for fn in list(handlers):
        fn(scene, None)


def _render(animation=False, scene=""):
    sc = data.scenes[scene]
    mode = _BEHAVIOR.get("mode", "ok")
    if mode == "fail_op":
        print("Error: No camera found in scene \"%s\"" % sc.name)
        return {"CANCELLED"}
    frame_time = float(_BEHAVIOR.get("frame_time", 0.02))
    samples = int(_BEHAVIOR.get("samples", 4))
    for frame in range(sc.frame_start, sc.frame_end + 1, sc.frame_step):
        sc.frame_current = frame
        out = sc.render.frame_path(frame=frame)
        if os.path.exists(out) and not sc.render.use_overwrite:
            print("skipping existing frame \"%s\"" % out)
            continue
        _call(app.handlers.render_pre, sc)
        if mode == "crash" and frame == sc.frame_start + 1:
            print("Segmentation fault")
            sys.stdout.flush()
            os._exit(139)
        for s in range(1, samples + 1):
            print("Fra:%d Mem:10M | Time:00:00.10 | Scene, ViewLayer | Sample %d/%d" % (frame, s, samples))
            sys.stdout.flush()
            time.sleep(frame_time / samples)
        if mode == "hang":
            time.sleep(120)
        _call(app.handlers.render_post, sc)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w") as fh:
            fh.write("frame %d\n" % frame)
        print("Saved: '%s'" % out)
        _call(app.handlers.render_write, sc)
    return {"FINISHED"}


class _RenderOps:
    render = staticmethod(_render)


class _Ops:
    render = _RenderOps()


app = _App()
data = _Data()
ops = _Ops()
context = _Context(False)


def load_blend(path):
    """Carrega o .blend simulado (JSON)."""
    global context
    with open(path, "r", encoding="utf-8") as fh:
        blend = json.load(fh)
    _BEHAVIOR.clear()
    _BEHAVIOR.update(blend.get("behavior", {}))
    data.filepath = path
    for name, scene_data in blend["scenes"].items():
        data.scenes[name] = _Scene(name, scene_data)
    context = _Context(blend.get("gpu_available", False))
    sys.modules[__name__].context = context
