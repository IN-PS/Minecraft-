# SPDX-License-Identifier: GPL-3.0-or-later
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty

ADDON_ID = __package__


class AUTORENDER_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    low_priority: BoolProperty(
        name="Prioridade baixa para o processo de renderização",
        description="Dá prioridade à interface do Blender principal (recomendado para manter a reprodução fluida)",
        default=True,
    )
    threads: IntProperty(
        name="Threads de CPU",
        description="Threads usadas pelo processo de renderização (0 = automático). "
                    "Deixar núcleos livres ajuda a reprodução da timeline",
        default=0, min=0, max=1024,
    )
    device_mode: EnumProperty(
        name="Dispositivo (Cycles)",
        items=(
            ("SCENE", "Igual à cena", "Usa o dispositivo configurado na cena e nas Preferências"),
            ("CPU", "Forçar CPU", "Deixa a GPU livre para a viewport/reprodução"),
        ),
        default="SCENE",
    )
    snapshot_location: EnumProperty(
        name="Cópia do projeto",
        items=(
            ("BLEND_DIR", "Pasta do .blend", "Cópia oculta ao lado do arquivo original "
             "(mais compatível com caminhos relativos e caches)"),
            ("TEMP", "Pasta temporária", "Cópia na pasta temporária do sistema"),
        ),
        default="BLEND_DIR",
    )
    cancel_grace: FloatProperty(
        name="Espera para cancelar (s)",
        description="Tempo aguardando o fim do frame atual antes de encerrar o processo à força",
        default=10.0, min=0.0, max=600.0,
    )
    keep_running_on_exit: BoolProperty(
        name="Continuar renderizando ao fechar o Blender",
        description="Se desativado, a renderização é encerrada quando o Blender principal for fechado",
        default=False,
    )
    blender_path: StringProperty(
        name="Executável do Blender",
        description="Deixe vazio para usar o mesmo executável desta sessão",
        subtype="FILE_PATH",
        default="",
    )

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.prop(self, "low_priority")
        col.prop(self, "threads")
        col.prop(self, "device_mode")
        col.prop(self, "snapshot_location")
        col.prop(self, "cancel_grace")
        col.prop(self, "keep_running_on_exit")
        col.prop(self, "blender_path")
        box = layout.box()
        box.label(text="Renderizar e reproduzir ao mesmo tempo disputa CPU, GPU e memória.", icon="INFO")
        box.label(text="A simultaneidade não é garantida e o FPS da reprodução pode cair bastante.")


def get_prefs(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon else None
