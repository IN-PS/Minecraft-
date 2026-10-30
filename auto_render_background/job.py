# SPDX-License-Identifier: GPL-3.0-or-later
"""Gerenciamento do processo de renderização em segundo plano.

Este módulo NÃO importa ``bpy``: ele só lida com ``subprocess``, arquivos de
status e cancelamento. Isso permite testá-lo fora do Blender e garante que a
lógica de processo não dependa da interface principal.

Comunicação (IPC) entre o Blender principal e o processo de renderização,
todos dentro de um diretório de trabalho temporário (``job_dir``):

* ``job.json``     -> configuração escrita pelo Blender principal, lida pelo worker.
* ``status.json``  -> estado escrito pelo worker (escrita atômica via os.replace).
* ``cancel.flag``  -> criado pelo Blender principal para pedir cancelamento seguro.
* ``render.log``   -> stdout/stderr completos do processo de renderização.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

# Estados exibidos na interface.
AGUARDANDO = "Aguardando"
RENDERIZANDO = "Renderizando"
CONCLUIDO = "Concluído"
ERRO = "Erro"
CANCELADO = "Cancelado"

ESTADOS_FINAIS = (CONCLUIDO, ERRO, CANCELADO)

JOB_FILE = "job.json"
STATUS_FILE = "status.json"
CANCEL_FILE = "cancel.flag"
LOG_FILE = "render.log"

# Quantos bytes do final do log são lidos a cada atualização.
_LOG_TAIL_BYTES = 8192

# Progresso dentro do frame atual (saída padrão do Blender).
_RE_FRAME = re.compile(r"Fra:\s*(-?\d+)")
_RE_CYCLES_SAMPLE = re.compile(r"Sample\s+(\d+)\s*/\s*(\d+)")
_RE_EEVEE_SAMPLE = re.compile(r"Rendering\s+(\d+)\s*/\s*(\d+)\s+samples")
_RE_ERROR_LINE = re.compile(r"(Error|ERROR|Traceback|Exception|Segmentation fault|Out of memory)")


class JobError(Exception):
    """Erro ao preparar ou iniciar uma renderização."""


def write_json_atomic(path, data):
    """Escreve JSON de forma atômica (arquivo temporário + os.replace)."""
    tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex[:8])
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    # No Windows, os.replace pode falhar se o leitor estiver com o arquivo
    # aberto naquele instante; tenta novamente algumas vezes.
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.01)


def read_json(path):
    """Lê JSON; retorna None se o arquivo não existir ou estiver inválido."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def read_log_tail(path, max_bytes=_LOG_TAIL_BYTES):
    """Retorna as últimas linhas do log (sem bloquear o arquivo)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    # Cycles atualiza a mesma linha com '\r'.
    text = text.replace("\r", "\n")
    return [line for line in text.split("\n") if line.strip()]


def parse_frame_fraction(lines, frame):
    """Fração (0..1) do frame ``frame`` já amostrada, a partir do log."""
    for line in reversed(lines):
        m_frame = _RE_FRAME.search(line)
        if not m_frame or int(m_frame.group(1)) != frame:
            continue
        m = _RE_CYCLES_SAMPLE.search(line) or _RE_EEVEE_SAMPLE.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            if total > 0:
                return max(0.0, min(1.0, done / float(total)))
    return 0.0


def last_error_lines(lines, count=3):
    errors = [line.strip() for line in lines if _RE_ERROR_LINE.search(line)]
    return errors[-count:]


def count_frames(frame_start, frame_end, frame_step):
    if frame_step < 1 or frame_end < frame_start:
        return 0
    return len(range(frame_start, frame_end + 1, frame_step))


class RenderJob:
    """Um processo de renderização em segundo plano.

    ``launcher`` é a lista de argumentos que inicia o Blender (normalmente
    ``[bpy.app.binary_path]``). Nos testes é substituída por um Blender falso.
    """

    def __init__(self, launcher, blend_path, worker_script, config,
                 threads=0, low_priority=True, cancel_grace=10.0,
                 kill_after=5.0, extra_args=None, snapshot_to_delete=None,
                 job_dir=None):
        if isinstance(launcher, str):
            launcher = [launcher]
        self.launcher = list(launcher)
        self.blend_path = blend_path
        self.worker_script = worker_script
        self.config = dict(config)
        self.threads = int(threads)
        self.low_priority = bool(low_priority)
        self.cancel_grace = float(cancel_grace)
        self.kill_after = float(kill_after)
        self.extra_args = list(extra_args or [])
        self.snapshot_to_delete = snapshot_to_delete
        self.job_dir = job_dir or tempfile.mkdtemp(prefix="autorender_")

        self.job_path = os.path.join(self.job_dir, JOB_FILE)
        self.status_path = os.path.join(self.job_dir, STATUS_FILE)
        self.cancel_path = os.path.join(self.job_dir, CANCEL_FILE)
        self.log_path = os.path.join(self.job_dir, LOG_FILE)

        self.process = None
        self._log_handle = None
        self.state = AGUARDANDO
        self.message = ""
        self.progress = 0.0
        self.frames_done = 0
        self.frames_total = count_frames(
            int(self.config.get("frame_start", 1)),
            int(self.config.get("frame_end", 1)),
            int(self.config.get("frame_step", 1)),
        )
        self.current_frame = None
        self.returncode = None
        self.started_at = None
        self.finished_at = None
        self.cancel_requested_at = None
        self._terminated_at = None
        self._killed = False
        self.worker_status = {}

    # ------------------------------------------------------------------ util
    @property
    def is_running(self):
        return self.state == RENDERIZANDO

    @property
    def is_finished(self):
        return self.state in ESTADOS_FINAIS

    @property
    def elapsed(self):
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)

    def build_command(self):
        cmd = list(self.launcher)
        cmd += ["-b", self.blend_path]
        cmd += self.extra_args
        if self.threads > 0:
            cmd += ["-t", str(self.threads)]
        cmd += ["--python-exit-code", "1", "-P", self.worker_script, "--", self.job_path]
        return cmd

    # ----------------------------------------------------------------- start
    def start(self):
        if self.process is not None:
            raise JobError("Esta renderização já foi iniciada.")
        if not os.path.isfile(self.blend_path):
            raise JobError("Arquivo .blend não encontrado: %s" % self.blend_path)
        if not os.path.isfile(self.worker_script):
            raise JobError("Script do worker não encontrado: %s" % self.worker_script)

        os.makedirs(self.job_dir, exist_ok=True)
        for path in (self.status_path, self.cancel_path):
            if os.path.exists(path):
                os.remove(path)

        config = dict(self.config)
        config.update({
            "status_path": self.status_path,
            "cancel_path": self.cancel_path,
            "parent_pid": os.getpid(),
        })
        write_json_atomic(self.job_path, config)

        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stderr": subprocess.STDOUT,
            "cwd": self.job_dir,
            "close_fds": True,
        }
        if sys.platform == "win32":
            flags = 0x00000200  # CREATE_NEW_PROCESS_GROUP
            flags |= 0x08000000  # CREATE_NO_WINDOW
            if self.low_priority:
                flags |= 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
            kwargs["creationflags"] = flags
        else:
            # Sessão própria: Ctrl+C no terminal do Blender não derruba o worker
            # e o cancelamento pode atingir o grupo de processos inteiro.
            kwargs["start_new_session"] = True

        self._log_handle = open(self.log_path, "wb")
        try:
            self.process = subprocess.Popen(self.build_command(), stdout=self._log_handle, **kwargs)
        except OSError as exc:
            self._close_log()
            raise JobError("Não foi possível iniciar o processo do Blender: %s" % exc)

        if self.low_priority and sys.platform != "win32":
            try:
                os.setpriority(os.PRIO_PROCESS, self.process.pid, 10)
            except (AttributeError, OSError):
                pass

        self.started_at = time.time()
        self.state = RENDERIZANDO
        self.message = "Iniciando processo de renderização..."
        return self.process.pid

    # ------------------------------------------------------------------ poll
    def poll(self):
        """Atualiza o estado. Retorna True se algo visível mudou."""
        if self.process is None or self.is_finished:
            return False

        before = (self.state, self.message, round(self.progress, 4), self.frames_done)

        status = read_json(self.status_path)
        if isinstance(status, dict):
            self.worker_status = status
            self.frames_done = int(status.get("frames_done", self.frames_done) or 0)
            if status.get("frames_total"):
                self.frames_total = int(status["frames_total"])
            self.current_frame = status.get("current_frame", self.current_frame)
            if status.get("message") and self.cancel_requested_at is None:
                self.message = status["message"]

        self._update_progress()

        returncode = self.process.poll()
        if returncode is None:
            self._escalate_cancel()
        else:
            self._finish(returncode)

        after = (self.state, self.message, round(self.progress, 4), self.frames_done)
        return before != after

    def _update_progress(self):
        if self.frames_total <= 0:
            return
        fraction = 0.0
        if self.current_frame is not None and self.frames_done < self.frames_total:
            fraction = parse_frame_fraction(read_log_tail(self.log_path), int(self.current_frame))
        value = (self.frames_done + fraction) / float(self.frames_total)
        # Nunca regride (o log pode oscilar entre camadas/visões).
        self.progress = max(self.progress, min(1.0, value))

    def _finish(self, returncode):
        self.returncode = returncode
        self.finished_at = time.time()
        self._close_log()
        worker_state = self.worker_status.get("state")

        if worker_state == "done" and returncode == 0:
            self.state = CONCLUIDO
            self.progress = 1.0
            self.message = "Renderização concluída em %s." % format_duration(self.elapsed)
        elif self.cancel_requested_at is not None or worker_state == "cancelled":
            self.state = CANCELADO
            self.message = "Renderização cancelada (%d de %d frames prontos)." % (
                self.frames_done, self.frames_total)
        else:
            self.state = ERRO
            detail = self.worker_status.get("message") if worker_state == "error" else ""
            if not detail:
                errors = last_error_lines(read_log_tail(self.log_path))
                detail = errors[-1] if errors else ""
            base = "O processo de renderização terminou inesperadamente (código %s)." % returncode
            self.message = (base + " " + detail).strip() if detail else base

        self._cleanup_snapshot()

    # ---------------------------------------------------------------- cancel
    def request_cancel(self):
        """Pede encerramento seguro: o worker para no fim do frame atual.

        Se o worker não responder dentro de ``cancel_grace`` segundos, o
        processo é terminado (e depois morto, se necessário) por ``poll()``.
        """
        if self.process is None or self.is_finished:
            return False
        if self.cancel_requested_at is None:
            self.cancel_requested_at = time.time()
            try:
                with open(self.cancel_path, "w", encoding="utf-8") as fh:
                    fh.write(str(self.cancel_requested_at))
            except OSError:
                # Sem como sinalizar: encerra direto.
                self.cancel_grace = 0.0
            self.message = "Cancelando... aguardando o fim do frame atual."
        self._escalate_cancel()
        return True

    def _escalate_cancel(self):
        if self.cancel_requested_at is None or self.process is None:
            return
        if self.process.poll() is not None:
            return
        now = time.time()
        if self._terminated_at is None and now - self.cancel_requested_at >= self.cancel_grace:
            self._terminated_at = now
            self.message = "Cancelando... encerrando o processo."
            self._signal(kill=False)
        elif (self._terminated_at is not None and not self._killed
              and now - self._terminated_at >= self.kill_after):
            self._killed = True
            self.message = "Cancelando... forçando o encerramento."
            self._signal(kill=True)

    def _signal(self, kill):
        proc = self.process
        try:
            if sys.platform != "win32":
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL if kill else signal.SIGTERM)
            elif kill:
                proc.kill()
            else:
                proc.terminate()
        except (OSError, ProcessLookupError):
            pass

    def kill_now(self, timeout=5.0):
        """Encerra imediatamente (usado ao fechar o Blender)."""
        if self.process is None or self.process.poll() is not None:
            return
        if self.cancel_requested_at is None:
            self.cancel_requested_at = time.time()
        self._signal(kill=False)
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._signal(kill=True)
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        self.poll()

    # --------------------------------------------------------------- cleanup
    def _close_log(self):
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None

    def _cleanup_snapshot(self):
        path = self.snapshot_to_delete
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass

    def remove_job_dir(self):
        if self.is_running:
            return
        shutil.rmtree(self.job_dir, ignore_errors=True)


def format_duration(seconds):
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh%02dm%02ds" % (h, m, s)
    if m:
        return "%dm%02ds" % (m, s)
    return "%ds" % s


class JobManager:
    """Garante que apenas uma renderização rode por vez nesta sessão."""

    def __init__(self):
        self.job = None

    @property
    def is_running(self):
        return self.job is not None and self.job.is_running

    def start(self, job):
        if self.is_running:
            raise JobError("Já existe uma renderização em andamento.")
        previous = self.job
        job.start()
        self.job = job
        if previous is not None:
            previous.remove_job_dir()
        return job

    def poll(self):
        return self.job.poll() if self.job is not None else False

    def cancel(self):
        return self.job.request_cancel() if self.job is not None else False

    def shutdown(self, keep_running=False):
        if self.job is not None and self.job.is_running and not keep_running:
            self.job.kill_now()
