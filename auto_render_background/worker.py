# SPDX-License-Identifier: GPL-3.0-or-later
"""Script executado DENTRO do processo Blender em segundo plano.

Uso (montado automaticamente pelo add-on):

    blender -b copia.blend --python-exit-code 1 -P worker.py -- job.json

Este arquivo é independente do add-on e da interface principal: ele só lê
``job.json``, aplica as configurações na cena, renderiza a animação e grava o
progresso em ``status.json``. O cancelamento é pedido pelo Blender principal
criando ``cancel.flag``; o worker verifica esse arquivo entre os frames.
"""

import json
import os
import sys
import time
import traceback
import uuid

import bpy

STATE = {
    "config": None,
    "frames_done": 0,
    "frames_seen": set(),
    "frames_total": 0,
    "current_frame": None,
    "devices": [],
    "warnings": [],
    "started": time.time(),
}


def log(msg):
    print("[AutoRender] %s" % msg)
    sys.stdout.flush()


def write_status(state, message="", **extra):
    cfg = STATE["config"]
    if not cfg:
        return
    data = {
        "state": state,
        "message": message,
        "frames_done": STATE["frames_done"],
        "frames_total": STATE["frames_total"],
        "current_frame": STATE["current_frame"],
        "devices": STATE["devices"],
        "warnings": STATE["warnings"],
        "pid": os.getpid(),
        "time": time.time(),
    }
    data.update(extra)
    path = cfg["status_path"]
    tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex[:8])
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.01)
    except OSError as exc:
        log("Falha ao gravar status: %s" % exc)


def parent_alive(pid):
    if not pid:
        return True
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel32.CloseHandle(handle)
            return code.value == 259  # STILL_ACTIVE
        except Exception:
            return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def stop_requested():
    cfg = STATE["config"]
    if os.path.exists(cfg["cancel_path"]):
        return "Cancelado pelo usuário."
    if cfg.get("stop_if_parent_dies") and not parent_alive(cfg.get("parent_pid")):
        return "O Blender principal foi fechado; renderização interrompida."
    return None


def exit_cancelled(reason):
    log(reason)
    write_status("cancelled", reason)
    sys.stdout.flush()
    # Sai sem desmontar o Blender no meio de uma renderização de animação.
    # O frame atual já foi gravado (chamado a partir de render_write/render_pre).
    os._exit(0)


# ----------------------------------------------------------------- handlers
def on_render_pre(scene, *_args):
    reason = stop_requested()
    if reason:
        exit_cancelled(reason)
    STATE["current_frame"] = scene.frame_current
    write_status("rendering", "Renderizando frame %d (%d/%d)" % (
        scene.frame_current, STATE["frames_done"] + 1, STATE["frames_total"]))


def on_render_post(scene, *_args):
    frame = scene.frame_current
    if frame not in STATE["frames_seen"]:
        STATE["frames_seen"].add(frame)
        STATE["frames_done"] = len(STATE["frames_seen"])
    write_status("rendering", "Frame %d concluído (%d/%d)" % (
        frame, STATE["frames_done"], STATE["frames_total"]))


def on_render_write(scene, *_args):
    # O frame já foi gravado em disco: ponto seguro para cancelar.
    reason = stop_requested()
    if reason:
        exit_cancelled(reason)


def on_render_cancel(scene, *_args):
    write_status("cancelled", "Renderização cancelada pelo Blender.")


# -------------------------------------------------------------------- setup
def configure_cycles_device(scene, cfg):
    """Detecta GPUs disponíveis neste processo e aplica o modo escolhido."""
    if scene.render.engine != "CYCLES":
        return
    mode = cfg.get("device_mode", "SCENE")
    if mode == "CPU":
        scene.cycles.device = "CPU"
        log("Dispositivo forçado: CPU")
        return

    addon = bpy.context.preferences.addons.get("cycles")
    if addon is None:
        return
    prefs = addon.preferences
    try:
        if hasattr(prefs, "refresh_devices"):
            prefs.refresh_devices()
        else:
            prefs.get_devices()
    except Exception as exc:  # driver ausente, etc.
        STATE["warnings"].append("Falha ao detectar dispositivos: %s" % exc)

    compute_type = getattr(prefs, "compute_device_type", "NONE") or "NONE"
    gpus = [d for d in prefs.devices if d.use and d.type != "CPU" and d.type == compute_type]
    STATE["devices"] = ["%s (%s)" % (d.name, d.type) for d in prefs.devices if d.use]
    log("Tipo de computação: %s | Dispositivos ativos: %s" % (
        compute_type, ", ".join(STATE["devices"]) or "nenhum"))

    if scene.cycles.device == "GPU" and (compute_type == "NONE" or not gpus):
        scene.cycles.device = "CPU"
        msg = "Nenhuma GPU ativa no processo secundário; usando CPU."
        STATE["warnings"].append(msg)
        log(msg)


def apply_config(cfg):
    scene = bpy.data.scenes.get(cfg["scene"])
    if scene is None:
        raise RuntimeError("Cena '%s' não encontrada no arquivo." % cfg["scene"])
    if bpy.context.window is not None:
        bpy.context.window.scene = scene

    render = scene.render
    scene.frame_start = int(cfg["frame_start"])
    scene.frame_end = int(cfg["frame_end"])
    scene.frame_step = int(cfg["frame_step"])
    render.filepath = cfg["output_path"]
    # Sem autorização explícita, nunca sobrescreve arquivos existentes.
    render.use_overwrite = bool(cfg.get("overwrite", False))

    configure_cycles_device(scene, cfg)

    STATE["frames_total"] = len(range(scene.frame_start, scene.frame_end + 1, scene.frame_step))
    log("Cena: %s | Motor: %s | Frames: %d-%d (passo %d) | %dx%d %d%% | Saída: %s" % (
        scene.name, render.engine, scene.frame_start, scene.frame_end, scene.frame_step,
        render.resolution_x, render.resolution_y, render.resolution_percentage,
        render.filepath))
    return scene


def install_handlers():
    handlers = bpy.app.handlers
    handlers.render_pre.append(on_render_pre)
    handlers.render_post.append(on_render_post)
    handlers.render_write.append(on_render_write)
    handlers.render_cancel.append(on_render_cancel)


def parse_args(argv):
    if "--" not in argv:
        raise RuntimeError("Uso: blender -b arquivo.blend -P worker.py -- job.json")
    args = argv[argv.index("--") + 1:]
    if not args:
        raise RuntimeError("Caminho do job.json não informado.")
    return args[0]


def main():
    job_path = parse_args(sys.argv)
    with open(job_path, "r", encoding="utf-8") as fh:
        STATE["config"] = json.load(fh)
    cfg = STATE["config"]

    try:
        write_status("starting", "Carregando cena...")
        scene = apply_config(cfg)
        reason = stop_requested()
        if reason:
            exit_cancelled(reason)
        install_handlers()
        write_status("rendering", "Renderizando %d frames..." % STATE["frames_total"])

        result = bpy.ops.render.render(animation=True, scene=scene.name)
        if "CANCELLED" in result:
            raise RuntimeError("O operador de renderização foi cancelado (verifique câmera e saída).")

        elapsed = time.time() - STATE["started"]
        # Frames pulados (arquivo já existente sem permissão para sobrescrever)
        # não disparam render_post; a animação terminou mesmo assim.
        write_status("done", "Concluído em %.1fs" % elapsed, elapsed=elapsed)
        log("Renderização concluída em %.1fs" % elapsed)
    except SystemExit:
        raise
    except BaseException as exc:
        tb = traceback.format_exc()
        log(tb)
        write_status("error", "%s: %s" % (type(exc).__name__, exc), traceback=tb)
        sys.stdout.flush()
        # Com --python-exit-code 1 o Blender retorna 1 ao propagar a exceção.
        raise


if __name__ == "__main__":
    main()
