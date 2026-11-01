# SPDX-License-Identifier: GPL-3.0-or-later
"""AutoRender Background - renderiza a animação em um processo separado do Blender."""

bl_info = {
    "name": "AutoRender Background",
    "author": "AutoRender Background",
    "version": (1, 1, 0),
    "blender": (3, 6, 0),
    "location": "Viewport 3D > Barra lateral (N) > Auto Render",
    "description": "Renderiza a animação em segundo plano com um clique, "
                   "mantendo a interface livre para reproduzir a timeline",
    "warning": "A reprodução simultânea depende de CPU, GPU e memória disponíveis",
    "doc_url": "",
    "category": "Render",
}

if "bpy" in locals():
    import importlib
    from . import job, scene_info, preferences, operators, ui
    for _mod in (job, scene_info, preferences, operators, ui):
        importlib.reload(_mod)

import bpy  # noqa: E402

from . import job, scene_info, preferences, operators, ui  # noqa: E402,F401


def register():
    bpy.utils.register_class(preferences.AUTORENDER_AddonPreferences)
    operators.register()
    ui.register()


def unregister():
    ui.unregister()
    operators.unregister()
    bpy.utils.unregister_class(preferences.AUTORENDER_AddonPreferences)
