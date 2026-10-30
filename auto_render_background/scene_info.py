# SPDX-License-Identifier: GPL-3.0-or-later
"""Leitura das configurações do projeto e validações (Blender principal)."""

import os
import tempfile
import uuid

import bpy

ENGINES = {
    "CYCLES": "Cycles",
    "BLENDER_EEVEE": "Eevee",
    "BLENDER_WORKBENCH": "Workbench",
}

# Cache da detecção de GPU (detectar dispositivos pode levar alguns segundos).
_GPU_CACHE = {"text": None}


def output_paths(scene):
    """Caminho de saída absoluto e diretório onde os arquivos serão gravados."""
    raw = scene.render.filepath
    abs_path = os.path.normpath(bpy.path.abspath(raw)) if raw else ""
    if raw.endswith(("/", "\\")):
        directory = abs_path
        abs_path = abs_path + os.sep
    else:
        directory = os.path.dirname(abs_path)
    return abs_path, directory


def frame_list(scene):
    return list(range(scene.frame_start, scene.frame_end + 1, max(1, scene.frame_step)))


def expected_files(scene):
    """Arquivos que a renderização vai gerar (usa a lógica do próprio Blender)."""
    render = scene.render
    frames = frame_list(scene)
    if not frames:
        return []
    if render.is_movie_format:
        return [render.frame_path(frame=frames[0])]
    return [render.frame_path(frame=f) for f in frames]


def existing_outputs(scene, limit=None):
    found = []
    for path in expected_files(scene):
        if os.path.exists(path):
            found.append(path)
            if limit and len(found) >= limit:
                break
    return found


def fps_value(scene):
    return scene.render.fps / (scene.render.fps_base or 1.0)


def summary(scene):
    render = scene.render
    scale = render.resolution_percentage / 100.0
    out_path, out_dir = output_paths(scene)
    count = len(range(scene.frame_start, scene.frame_end + 1, max(1, scene.frame_step)))
    return {
        "scene": scene.name,
        "engine": ENGINES.get(render.engine, render.engine),
        "resolution": "%dx%d (%d%%)" % (
            int(render.resolution_x * scale), int(render.resolution_y * scale),
            render.resolution_percentage),
        "fps": "%.3g" % fps_value(scene),
        "frames": "%d-%d (%d frames)" % (scene.frame_start, scene.frame_end, count),
        "format": render.image_settings.file_format + (
            " / %s" % render.ffmpeg.format if render.is_movie_format and
            render.image_settings.file_format == "FFMPEG" else ""),
        "output_path": out_path,
        "output_dir": out_dir,
        "is_movie": render.is_movie_format,
    }


def gpu_description(refresh=False):
    """Texto curto com a configuração de GPU do Cycles (Preferências)."""
    if _GPU_CACHE["text"] is not None and not refresh:
        return _GPU_CACHE["text"]
    addon = bpy.context.preferences.addons.get("cycles")
    if addon is None:
        text = "Cycles desativado"
    else:
        prefs = addon.preferences
        if refresh:
            try:
                if hasattr(prefs, "refresh_devices"):
                    prefs.refresh_devices()
                else:
                    prefs.get_devices()
            except Exception as exc:
                _GPU_CACHE["text"] = "Falha na detecção: %s" % exc
                return _GPU_CACHE["text"]
        compute = getattr(prefs, "compute_device_type", "NONE") or "NONE"
        gpus = [d.name for d in prefs.devices if d.use and d.type == compute and d.type != "CPU"]
        if compute == "NONE" or not gpus:
            text = "Somente CPU (sem GPU ativa nas Preferências)"
        else:
            text = "%s: %s" % (compute, ", ".join(gpus))
    _GPU_CACHE["text"] = text
    return text


def validate_scene(scene):
    """Retorna lista de erros que impedem a renderização."""
    errors = []
    render = scene.render
    if not bpy.data.is_saved or not bpy.data.filepath:
        errors.append("Salve o projeto (.blend) antes de renderizar.")
    if scene.frame_end < scene.frame_start:
        errors.append("Intervalo de frames inválido.")
    uses_sequencer = bool(render.use_sequencer and scene.sequence_editor and
                          len(scene.sequence_editor.sequences_all) > 0)
    if scene.camera is None and not uses_sequencer:
        errors.append("A cena não possui câmera ativa.")
    if render.engine not in ENGINES:
        errors.append("Motor '%s' não suportado (use Cycles ou Eevee)." % render.engine)
    if not render.filepath.strip():
        errors.append("Defina o caminho de saída em Propriedades de Saída.")
    return errors


def validate_output_dir(directory):
    """Cria (se preciso) e testa escrita no diretório de saída."""
    if not directory:
        return "Diretório de saída vazio."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        return "Não foi possível criar o diretório de saída: %s" % exc
    if not os.path.isdir(directory):
        return "O caminho de saída não é um diretório: %s" % directory
    probe = os.path.join(directory, ".autorender_%s.tmp" % uuid.uuid4().hex[:8])
    try:
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as exc:
        return "Sem permissão de escrita no diretório de saída: %s" % exc
    return None


def snapshot_path(location):
    """Caminho da cópia do .blend usada pelo processo secundário."""
    blend = bpy.data.filepath
    stem = os.path.splitext(os.path.basename(blend))[0]
    name = ".%s.autorender_%s.blend" % (stem, uuid.uuid4().hex[:8])
    if location == "BLEND_DIR":
        directory = os.path.dirname(blend)
        if os.access(directory, os.W_OK):
            return os.path.join(directory, name)
    return os.path.join(tempfile.gettempdir(), name.lstrip("."))


def save_snapshot(path):
    """Grava uma cópia do estado atual sem alterar o arquivo original."""
    result = bpy.ops.wm.save_as_mainfile(
        filepath=path, copy=True, relative_remap=True, compress=False, check_existing=False)
    if "FINISHED" not in result or not os.path.isfile(path):
        raise RuntimeError("Falha ao gravar a cópia temporária do projeto.")
    return path


def autoexec_args():
    """Replica no processo secundário a permissão de scripts da sessão atual."""
    if getattr(bpy.app, "autoexec_fail", False):
        return ["-Y"]  # scripts foram bloqueados nesta sessão
    if bpy.context.preferences.filepaths.use_scripts_auto_execute:
        return []
    return ["-y"]  # usuário permitiu scripts manualmente para este arquivo
