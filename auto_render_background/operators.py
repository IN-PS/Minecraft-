# SPDX-License-Identifier: GPL-3.0-or-later
import atexit
import os

import bpy
from bpy.props import BoolProperty
from bpy.types import Operator

from . import scene_info
from .job import JobError, JobManager, RenderJob
from .preferences import get_prefs

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER_SCRIPT = os.path.join(ADDON_DIR, "worker.py")
POLL_INTERVAL = 0.5

# Uma única renderização por sessão do Blender.
MANAGER = JobManager()


# ------------------------------------------------------------------ timer
def _redraw_ui():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type in {"VIEW_3D", "SEQUENCE_EDITOR"}:
                area.tag_redraw()


def _tick():
    job = MANAGER.job
    if job is None:
        return None
    try:
        changed = MANAGER.poll()
    except Exception as exc:  # nunca deixar o timer quebrar a interface
        print("[AutoRender] Falha ao consultar o processo: %s" % exc)
        changed = True
    if changed:
        _redraw_ui()
    if job.is_finished:
        print("[AutoRender] %s: %s (log: %s)" % (job.state, job.message, job.log_path))
        return None
    return POLL_INTERVAL


def ensure_timer():
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=POLL_INTERVAL, persistent=True)


def _on_exit():
    job = MANAGER.job
    if job is not None:
        MANAGER.shutdown(keep_running=job.config.get("keep_running_on_exit", False))


# -------------------------------------------------------------- operators
class AUTORENDER_OT_renderizar(Operator):
    """Inicia a renderização da animação em um processo do Blender separado"""
    bl_idname = "autorender.renderizar"
    bl_label = "Renderizar agora"
    bl_options = {"REGISTER"}

    sobrescrever: BoolProperty(
        name="Sobrescrever arquivos existentes",
        description="Autoriza substituir frames/vídeos que já existem no diretório de saída",
        default=False,
        options={"SKIP_SAVE"},
    )
    salvar_antes: BoolProperty(
        name="Salvar o projeto antes de renderizar",
        description="Salva o .blend original. Se desmarcado, o estado atual é renderizado "
                    "a partir de uma cópia, sem alterar o arquivo original",
        default=True,
        options={"SKIP_SAVE"},
    )

    @classmethod
    def poll(cls, context):
        return not MANAGER.is_running and context.scene is not None

    def _collect_errors(self, scene):
        errors = scene_info.validate_scene(scene)
        if not errors:
            _out_path, out_dir = scene_info.output_paths(scene)
            err = scene_info.validate_output_dir(out_dir)
            if err:
                errors.append(err)
        return errors

    def invoke(self, context, event):
        scene = context.scene
        errors = self._collect_errors(scene)
        if errors:
            self.report({"ERROR"}, " | ".join(errors))
            return {"CANCELLED"}
        self._existing = scene_info.existing_outputs(scene)
        self._dirty = bpy.data.is_dirty
        if self._existing or self._dirty:
            return context.window_manager.invoke_props_dialog(self, width=460)
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        existing = getattr(self, "_existing", [])
        if existing:
            box = layout.box()
            box.label(text="%d arquivo(s) de saída já existem:" % len(existing), icon="ERROR")
            for path in existing[:3]:
                box.label(text=os.path.basename(path))
            if len(existing) > 3:
                box.label(text="...")
            box.prop(self, "sobrescrever")
        if getattr(self, "_dirty", False):
            box = layout.box()
            box.label(text="O projeto tem alterações não salvas.", icon="FILE_BLEND")
            box.prop(self, "salvar_antes")
            if not self.salvar_antes:
                box.label(text="O estado atual será renderizado a partir de uma cópia.")

    def execute(self, context):
        scene = context.scene
        if MANAGER.is_running:
            self.report({"WARNING"}, "Já existe uma renderização em andamento.")
            return {"CANCELLED"}

        errors = self._collect_errors(scene)
        if errors:
            self.report({"ERROR"}, " | ".join(errors))
            return {"CANCELLED"}

        existing = scene_info.existing_outputs(scene, limit=1)
        if existing and not self.sobrescrever:
            self.report({"ERROR"}, "Já existem arquivos de saída (ex.: %s). Autorize a "
                        "sobrescrita ou altere o caminho de saída." % os.path.basename(existing[0]))
            return {"CANCELLED"}

        if bpy.data.is_dirty and self.salvar_antes:
            try:
                bpy.ops.wm.save_mainfile()
            except RuntimeError as exc:
                self.report({"ERROR"}, "Falha ao salvar o projeto: %s" % exc)
                return {"CANCELLED"}

        prefs = get_prefs(context)
        launcher = (bpy.path.abspath(prefs.blender_path) if prefs and prefs.blender_path
                    else bpy.app.binary_path)
        if not launcher or not os.path.isfile(launcher):
            self.report({"ERROR"}, "Executável do Blender não encontrado: %s" % launcher)
            return {"CANCELLED"}

        info = scene_info.summary(scene)
        snapshot = None
        try:
            snapshot = scene_info.save_snapshot(
                scene_info.snapshot_path(prefs.snapshot_location if prefs else "BLEND_DIR"))
            keep = bool(prefs and prefs.keep_running_on_exit)
            speed_mode = prefs.speed_mode if prefs else "RAPIDO"
            config = {
                "scene": scene.name,
                "frame_start": scene.frame_start,
                "frame_end": scene.frame_end,
                "frame_step": max(1, scene.frame_step),
                "output_path": info["output_path"],
                "output_dir": info["output_dir"],
                "is_movie": info["is_movie"],
                "overwrite": bool(self.sobrescrever),
                "device_mode": prefs.device_mode if prefs else "SCENE",
                "speed_mode": speed_mode,
                "keep_running_on_exit": keep,
                "stop_if_parent_dies": not keep,
                "original_blend": bpy.data.filepath,
            }
            job = RenderJob(
                launcher=[launcher],
                blend_path=snapshot,
                worker_script=WORKER_SCRIPT,
                config=config,
                threads=prefs.threads if prefs else 0,
                low_priority=speed_mode == "EQUILIBRADO",
                cancel_grace=prefs.cancel_grace if prefs else 10.0,
                extra_args=scene_info.autoexec_args(),
                snapshot_to_delete=snapshot,
            )
            MANAGER.start(job)
        except (JobError, RuntimeError, OSError) as exc:
            if snapshot and os.path.isfile(snapshot):
                try:
                    os.remove(snapshot)
                except OSError:
                    pass
            self.report({"ERROR"}, "Não foi possível iniciar a renderização: %s" % exc)
            return {"CANCELLED"}

        ensure_timer()
        self.report({"INFO"}, "Renderização iniciada: %s, %s, %s fps, %s → %s" % (
            info["frames"], info["resolution"], info["fps"], info["format"], info["output_dir"]))
        return {"FINISHED"}


class AUTORENDER_OT_cancelar(Operator):
    """Solicita o encerramento seguro da renderização (ao fim do frame atual)"""
    bl_idname = "autorender.cancelar"
    bl_label = "Cancelar"

    @classmethod
    def poll(cls, context):
        return MANAGER.is_running

    def execute(self, context):
        if MANAGER.cancel():
            ensure_timer()
            self.report({"INFO"}, "Cancelamento solicitado.")
            return {"FINISHED"}
        return {"CANCELLED"}


def _target_output_dir(context):
    job = MANAGER.job
    if job is not None and job.config.get("output_dir"):
        return job.config["output_dir"]
    if context.scene is not None and context.scene.render.filepath:
        return scene_info.output_paths(context.scene)[1]
    return ""


class AUTORENDER_OT_abrir_pasta(Operator):
    """Abre o diretório onde os frames ou o vídeo são salvos"""
    bl_idname = "autorender.abrir_pasta"
    bl_label = "Abrir pasta"

    @classmethod
    def poll(cls, context):
        return bool(_target_output_dir(context))

    def execute(self, context):
        directory = _target_output_dir(context)
        if not os.path.isdir(directory):
            self.report({"ERROR"}, "Diretório ainda não existe: %s" % directory)
            return {"CANCELLED"}
        bpy.ops.wm.path_open(filepath=directory)
        return {"FINISHED"}


class AUTORENDER_OT_abrir_log(Operator):
    """Abre o log completo do processo de renderização"""
    bl_idname = "autorender.abrir_log"
    bl_label = "Abrir log"

    @classmethod
    def poll(cls, context):
        job = MANAGER.job
        return job is not None and os.path.isfile(job.log_path)

    def execute(self, context):
        bpy.ops.wm.path_open(filepath=MANAGER.job.log_path)
        return {"FINISHED"}


class AUTORENDER_OT_detectar_gpu(Operator):
    """Detecta novamente as GPUs configuradas para o Cycles"""
    bl_idname = "autorender.detectar_gpu"
    bl_label = "Detectar GPU"

    def execute(self, context):
        self.report({"INFO"}, scene_info.gpu_description(refresh=True))
        return {"FINISHED"}


classes = (
    AUTORENDER_OT_renderizar,
    AUTORENDER_OT_cancelar,
    AUTORENDER_OT_abrir_pasta,
    AUTORENDER_OT_abrir_log,
    AUTORENDER_OT_detectar_gpu,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    atexit.register(_on_exit)


def unregister():
    atexit.unregister(_on_exit)
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    # Desativar o add-on não pode deixar um processo órfão sem controle.
    _on_exit()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
