"""Teste de fumaça do add-on (registro, operador, validações) com um bpy stub.

Não substitui o teste dentro do Blender 3.6.22 (veja README), mas garante que
os módulos importam, que register()/unregister() são simétricos e que o fluxo
do botão RENDERIZAR AGORA chega a iniciar o processo secundário.
"""

import importlib
import json
import os
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def build_stub_bpy(blend_path):
    bpy = types.ModuleType("bpy")
    registered = []

    class _Base:
        def report(self, level, msg):
            self.reports.append((set(level), msg))

    class Operator(_Base):
        pass

    class Panel:
        pass

    class AddonPreferences:
        pass

    class WindowManager:
        pass

    def _prop(kind):
        def factory(**kw):
            return (kind, kw)
        return factory

    bpy.types = types.SimpleNamespace(Operator=Operator, Panel=Panel,
                                      AddonPreferences=AddonPreferences,
                                      WindowManager=WindowManager)
    bpy.props = types.SimpleNamespace(**{n: _prop(n) for n in (
        "BoolProperty", "EnumProperty", "FloatProperty", "IntProperty", "StringProperty")})

    def register_class(cls):
        assert cls not in registered, cls
        registered.append(cls)

    def unregister_class(cls):
        registered.remove(cls)

    bpy.utils = types.SimpleNamespace(register_class=register_class,
                                      unregister_class=unregister_class)

    timers = []
    bpy.app = types.SimpleNamespace(
        binary_path=sys.executable, autoexec_fail=False,
        timers=types.SimpleNamespace(
            register=lambda fn, **kw: timers.append(fn),
            unregister=lambda fn: timers.remove(fn),
            is_registered=lambda fn: fn in timers),
    )

    def abspath(path):
        if path.startswith("//"):
            return os.path.join(os.path.dirname(blend_path), path[2:])
        return path

    bpy.path = types.SimpleNamespace(abspath=abspath)
    bpy.context = types.SimpleNamespace(preferences=types.SimpleNamespace(
        filepaths=types.SimpleNamespace(use_scripts_auto_execute=True), addons={}))
    bpy.data = types.SimpleNamespace(is_saved=True, is_dirty=False, filepath=blend_path)

    saved = []

    def save_as_mainfile(filepath, copy, **kw):
        assert copy is True, "o arquivo original nunca pode ser substituído"
        # O .blend "simulado" é o JSON que o fake_blender entende.
        with open(filepath, "w") as fh:
            json.dump({"scenes": {"Scene": {"frame_start": 1, "frame_end": 3}}}, fh)
        saved.append(filepath)
        return {"FINISHED"}

    bpy.ops = types.SimpleNamespace(wm=types.SimpleNamespace(
        save_as_mainfile=save_as_mainfile,
        save_mainfile=lambda: {"FINISHED"},
        path_open=lambda filepath: {"FINISHED"}))
    bpy._registered = registered
    bpy._timers = timers
    bpy._saved = saved
    return bpy


class FakeRender:
    def __init__(self, filepath):
        self.filepath = filepath
        self.engine = "CYCLES"
        self.use_sequencer = False
        self.is_movie_format = False
        self.resolution_x, self.resolution_y, self.resolution_percentage = 1920, 1080, 50
        self.fps, self.fps_base = 24, 1.0
        self.image_settings = types.SimpleNamespace(file_format="PNG")
        self.ffmpeg = types.SimpleNamespace(format="MPEG4")

    def frame_path(self, frame=0):
        return self._abspath(self.filepath) + "%04d.png" % frame


class FakeScene:
    def __init__(self, filepath, abspath):
        self.name = "Scene"
        self.frame_start, self.frame_end, self.frame_step = 1, 3, 1
        self.camera = object()
        self.sequence_editor = None
        self.render = FakeRender(filepath)
        self.render._abspath = abspath


class AddonStubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="autorender_stub_")
        self.blend = os.path.join(self.tmp.name, "projeto.blend")
        with open(self.blend, "w") as fh:
            fh.write("original")
        self.bpy = build_stub_bpy(self.blend)
        sys.modules["bpy"] = self.bpy
        sys.modules["bpy.props"] = self.bpy.props
        sys.modules["bpy.types"] = self.bpy.types
        sys.path.insert(0, ROOT)
        for name in list(sys.modules):
            if name.startswith("auto_render_background"):
                del sys.modules[name]
        self.addon = importlib.import_module("auto_render_background")
        # O operador usa o fake_blender no lugar do executável real.
        self.prefs = types.SimpleNamespace(
            blender_path="", snapshot_location="BLEND_DIR", keep_running_on_exit=False,
            device_mode="SCENE", threads=0, low_priority=True, cancel_grace=5.0)

    def tearDown(self):
        ops = sys.modules.get("auto_render_background.operators")
        if ops is not None and ops.MANAGER.job is not None:
            ops.MANAGER.shutdown()
            ops.MANAGER.job.remove_job_dir()
        for name in ("bpy", "bpy.props", "bpy.types"):
            sys.modules.pop(name, None)
        sys.path.remove(ROOT)
        self.tmp.cleanup()

    def test_register_unregister(self):
        self.addon.register()
        names = [c.__name__ for c in self.bpy._registered]
        for expected in ("AUTORENDER_AddonPreferences", "AUTORENDER_OT_renderizar",
                         "AUTORENDER_OT_cancelar", "AUTORENDER_OT_abrir_pasta",
                         "AUTORENDER_PT_view3d", "AUTORENDER_PT_sequencer"):
            self.assertIn(expected, names)
        self.assertTrue(hasattr(self.bpy.types.WindowManager, "autorender_progresso"))
        self.addon.unregister()
        self.assertEqual(self.bpy._registered, [])
        self.assertFalse(hasattr(self.bpy.types.WindowManager, "autorender_progresso"))

    def test_validacoes(self):
        si = self.addon.scene_info
        scene = FakeScene("//render/", self.bpy.path.abspath)
        out_path, out_dir = si.output_paths(scene)
        self.assertEqual(out_dir, os.path.join(self.tmp.name, "render"))
        self.assertTrue(out_path.endswith(os.sep))
        self.assertEqual(si.validate_scene(scene), [])
        self.assertIsNone(si.validate_output_dir(out_dir))
        self.assertTrue(os.path.isdir(out_dir))
        self.assertEqual(os.listdir(out_dir), [], "teste de escrita não pode deixar lixo")

        scene.camera = None
        self.assertIn("A cena não possui câmera ativa.", si.validate_scene(scene))
        self.bpy.data.is_saved = False
        self.assertTrue(any("Salve" in e for e in si.validate_scene(scene)))

        blocker = os.path.join(self.tmp.name, "arquivo")
        open(blocker, "w").close()
        self.assertIsNotNone(si.validate_output_dir(os.path.join(blocker, "sub")))

    def test_botao_renderizar_inicia_processo(self):
        ops = self.addon.operators
        si = self.addon.scene_info
        ops.get_prefs = lambda context=None: self.prefs
        # Usa o Blender simulado em vez do executável real.
        fake_launcher = [sys.executable, os.path.join(HERE, "fake_blender.py")]
        orig_init = ops.RenderJob.__init__

        def init(job_self, launcher, *a, **kw):
            orig_init(job_self, fake_launcher, *a, **kw)

        ops.RenderJob.__init__ = init
        try:
            scene = FakeScene("//render/", self.bpy.path.abspath)
            context = types.SimpleNamespace(scene=scene, window_manager=None)
            op = ops.AUTORENDER_OT_renderizar()
            op.reports, op.sobrescrever, op.salvar_antes = [], False, True
            self.assertTrue(ops.AUTORENDER_OT_renderizar.poll(context))
            self.assertEqual(op.execute(context), {"FINISHED"}, op.reports)
            self.assertFalse(ops.AUTORENDER_OT_renderizar.poll(context),
                             "o botão deve ficar bloqueado durante a renderização")
            self.assertEqual(op.execute(context), {"CANCELLED"})
            self.assertTrue(self.bpy.app.timers.is_registered(ops._tick))

            job = ops.MANAGER.job
            snapshot = self.bpy._saved[0]
            self.assertEqual(os.path.dirname(snapshot), self.tmp.name)
            self.assertTrue(os.path.basename(snapshot).startswith(".projeto.autorender_"))
            deadline = time.time() + 20
            while not job.is_finished and time.time() < deadline:
                job.poll()
                time.sleep(0.05)
            self.assertEqual(job.state, "Concluído", job.message)
            self.assertFalse(os.path.exists(snapshot))
            with open(self.blend) as fh:
                self.assertEqual(fh.read(), "original", "o arquivo original foi alterado")

            # Agora os arquivos existem: sem autorização, não pode iniciar.
            self.assertTrue(si.existing_outputs(scene))
            op2 = ops.AUTORENDER_OT_renderizar()
            op2.reports, op2.sobrescrever, op2.salvar_antes = [], False, True
            self.assertEqual(op2.execute(context), {"CANCELLED"})
            self.assertIn("Já existem arquivos", op2.reports[-1][1])
        finally:
            ops.RenderJob.__init__ = orig_init


if __name__ == "__main__":
    unittest.main()
