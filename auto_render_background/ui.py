# SPDX-License-Identifier: GPL-3.0-or-later
import bpy
from bpy.props import FloatProperty
from bpy.types import Panel

from . import scene_info
from .job import AGUARDANDO, CANCELADO, CONCLUIDO, ERRO, RENDERIZANDO, format_duration
from .operators import MANAGER
from .preferences import get_prefs

STATE_ICONS = {
    AGUARDANDO: "TIME",
    RENDERIZANDO: "RENDER_ANIMATION",
    CONCLUIDO: "CHECKMARK",
    ERRO: "ERROR",
    CANCELADO: "CANCEL",
}


def _progress_get(_self):
    job = MANAGER.job
    return job.progress * 100.0 if job is not None else 0.0


def _wrap(layout, text, width=38):
    """Quebra textos longos em várias linhas (labels não quebram sozinhos)."""
    line = ""
    for word in text.split():
        if line and len(line) + len(word) + 1 > width:
            layout.label(text=line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        layout.label(text=line)


class AutoRenderPanelMixin:
    bl_label = "AUTO RENDER"
    bl_region_type = "UI"
    bl_category = "Auto Render"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        job = MANAGER.job
        running = MANAGER.is_running

        # Velocidade (fica salva nas preferências do add-on)
        prefs = get_prefs(context)
        if prefs is not None:
            row = layout.row()
            row.enabled = not running
            row.prop(prefs, "speed_mode", text="Velocidade")

        # Botão principal
        row = layout.row()
        row.scale_y = 1.8
        row.enabled = not running
        row.operator("autorender.renderizar", text="RENDERIZAR AGORA", icon="RENDER_ANIMATION")

        # Status
        state = job.state if job is not None else AGUARDANDO
        box = layout.box()
        row = box.row()
        row.label(text="STATUS:")
        row.label(text=state, icon=STATE_ICONS.get(state, "INFO"))
        if job is not None and job.message:
            col = box.column(align=True)
            col.scale_y = 0.8
            _wrap(col, job.message)

        # Progresso
        col = box.column(align=True)
        col.label(text="PROGRESSO:")
        col.prop(context.window_manager, "autorender_progresso", text="", slider=True)
        if job is not None:
            detail = "Frames: %d / %d" % (job.frames_done, job.frames_total)
            if job.started_at:
                detail += "   Tempo: %s" % format_duration(job.elapsed)
            col.label(text=detail)

        row = box.row(align=True)
        sub = row.row(align=True)
        sub.enabled = running
        sub.operator("autorender.cancelar", text="CANCELAR", icon="CANCEL")
        row.operator("autorender.abrir_pasta", text="ABRIR PASTA", icon="FILE_FOLDER")
        if job is not None and job.state == ERRO:
            box.operator("autorender.abrir_log", text="Abrir log", icon="TEXT")

        # Configurações capturadas
        if scene is not None:
            info = scene_info.summary(scene)
            box = layout.box()
            box.label(text="Projeto", icon="SCENE_DATA")
            col = box.column(align=True)
            col.scale_y = 0.8
            col.label(text="Cena: %s" % info["scene"])
            col.label(text="Motor: %s" % info["engine"])
            col.label(text="Resolução: %s" % info["resolution"])
            col.label(text="FPS: %s" % info["fps"])
            col.label(text="Frames: %s" % info["frames"])
            col.label(text="Formato: %s" % info["format"])
            col.label(text="Saída: %s" % (info["output_dir"] or "(não definida)"))
            if scene.render.engine == "CYCLES":
                row = box.row(align=True)
                row.label(text="GPU: %s" % scene_info.gpu_description(), icon="SETTINGS")
                row.operator("autorender.detectar_gpu", text="", icon="FILE_REFRESH")
            if not bpy.data.is_saved:
                box.label(text="Projeto ainda não foi salvo.", icon="ERROR")

        col = layout.column(align=True)
        col.scale_y = 0.75
        col.label(text="Reproduzir durante a renderização pode", icon="INFO")
        col.label(text="reduzir bastante o FPS da timeline.")


class AUTORENDER_PT_view3d(AutoRenderPanelMixin, Panel):
    bl_idname = "AUTORENDER_PT_view3d"
    bl_space_type = "VIEW_3D"


class AUTORENDER_PT_sequencer(AutoRenderPanelMixin, Panel):
    bl_idname = "AUTORENDER_PT_sequencer"
    bl_space_type = "SEQUENCE_EDITOR"


classes = (AUTORENDER_PT_view3d, AUTORENDER_PT_sequencer)


def register():
    bpy.types.WindowManager.autorender_progresso = FloatProperty(
        name="Progresso",
        subtype="PERCENTAGE",
        min=0.0, max=100.0, precision=1,
        get=_progress_get,
    )
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.WindowManager.autorender_progresso
