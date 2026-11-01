"""Testes do gerenciamento de processo + worker real, usando um Blender simulado.

Execute a partir da raiz do repositório:

    python -m unittest discover -s tests -v
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ADDON = os.path.join(ROOT, "auto_render_background")
sys.path.insert(0, ADDON)

import job as jobmod  # noqa: E402  (módulo sem dependência de bpy)

FAKE_BLENDER = [sys.executable, os.path.join(HERE, "fake_blender.py")]
WORKER = os.path.join(ADDON, "worker.py")


def wait(job, timeout=30.0):
    deadline = time.time() + timeout
    while not job.is_finished:
        if time.time() > deadline:
            job.kill_now()
            raise AssertionError("Timeout aguardando o job (estado: %s)" % job.state)
        job.poll()
        time.sleep(0.05)
    return job


class RenderJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="autorender_test_")
        self.dir = self.tmp.name
        self.out_dir = os.path.join(self.dir, "render")
        self.jobs = []

    def tearDown(self):
        for j in self.jobs:
            if j.process is not None and j.process.poll() is None:
                j.kill_now()
            j.remove_job_dir()
        self.tmp.cleanup()

    def make_job(self, frames=(1, 5, 1), behavior=None, overwrite=False, scene="Scene",
                 engine="CYCLES", device="CPU", gpu_available=False, extra_config=None, **kwargs):
        blend = os.path.join(self.dir, ".projeto.autorender_test.blend")
        with open(blend, "w") as fh:
            json.dump({
                "scenes": {"Scene": {"frame_start": frames[0], "frame_end": frames[1],
                                     "frame_step": frames[2], "engine": engine, "device": device}},
                "gpu_available": gpu_available,
                "behavior": behavior or {},
            }, fh)
        config = {
            "scene": scene, "frame_start": frames[0], "frame_end": frames[1],
            "frame_step": frames[2], "output_path": os.path.join(self.out_dir, "frame_####"),
            "output_dir": self.out_dir, "overwrite": overwrite, "device_mode": "SCENE",
            "stop_if_parent_dies": True, "speed_mode": "EQUILIBRADO",
        }
        config.update(extra_config or {})
        j = jobmod.RenderJob(FAKE_BLENDER, blend, WORKER, config,
                             snapshot_to_delete=blend, **kwargs)
        self.jobs.append(j)
        return j

    def outputs(self):
        if not os.path.isdir(self.out_dir):
            return []
        return sorted(os.listdir(self.out_dir))

    # ------------------------------------------------------------- tests
    def test_render_completo(self):
        j = self.make_job(frames=(1, 5, 1), behavior={"frame_time": 0.05})
        self.assertEqual(j.state, jobmod.AGUARDANDO)
        j.start()
        self.assertEqual(j.state, jobmod.RENDERIZANDO)
        wait(j)
        self.assertEqual(j.state, jobmod.CONCLUIDO, j.message)
        self.assertEqual(j.returncode, 0)
        self.assertEqual(j.progress, 1.0)
        self.assertEqual(j.frames_done, 5)
        self.assertEqual(self.outputs(), ["frame_%04d.png" % f for f in range(1, 6)])
        self.assertFalse(os.path.exists(j.blend_path), "a cópia temporária deve ser apagada")

    def test_frame_step(self):
        j = self.make_job(frames=(10, 20, 5))
        self.assertEqual(j.frames_total, 3)
        j.start()
        wait(j)
        self.assertEqual(j.state, jobmod.CONCLUIDO, j.message)
        self.assertEqual(self.outputs(), ["frame_0010.png", "frame_0015.png", "frame_0020.png"])

    def test_progresso_parcial_e_monotonico(self):
        j = self.make_job(frames=(1, 10, 1), behavior={"frame_time": 0.2, "samples": 4})
        j.start()
        seen = []
        while not j.is_finished:
            j.poll()
            seen.append(j.progress)
            time.sleep(0.03)
        self.assertEqual(j.state, jobmod.CONCLUIDO, j.message)
        self.assertEqual(seen, sorted(seen), "progresso nunca deve regredir")
        intermediate = [p for p in seen if 0.0 < p < 1.0]
        self.assertTrue(intermediate, "deveria haver progresso intermediário")
        # Progresso fracionário dentro de um frame (amostras lidas do log).
        self.assertTrue(any(abs(p * 10 - round(p * 10)) > 0.01 for p in intermediate))

    def test_cancelamento_seguro_entre_frames(self):
        j = self.make_job(frames=(1, 50, 1), behavior={"frame_time": 0.15}, cancel_grace=20.0)
        j.start()
        while j.frames_done < 2:
            j.poll()
            time.sleep(0.03)
            self.assertFalse(j.is_finished, j.message)
        t0 = time.time()
        self.assertTrue(j.request_cancel())
        wait(j)
        self.assertEqual(j.state, jobmod.CANCELADO, j.message)
        self.assertEqual(j.returncode, 0, "o worker deve sair sozinho, sem ser morto")
        self.assertLess(time.time() - t0, 5.0)
        self.assertEqual(j.worker_status.get("state"), "cancelled")
        self.assertLess(len(self.outputs()), 50)
        # Todo frame gravado está completo (cancelamento só após render_write).
        for name in self.outputs():
            with open(os.path.join(self.out_dir, name)) as fh:
                self.assertTrue(fh.read().startswith("frame "))

    def test_cancelamento_forcado_quando_worker_nao_responde(self):
        j = self.make_job(frames=(1, 3, 1), behavior={"mode": "hang"},
                          cancel_grace=0.5, kill_after=1.0)
        j.start()
        time.sleep(0.5)
        j.request_cancel()
        t0 = time.time()
        wait(j, timeout=15)
        self.assertEqual(j.state, jobmod.CANCELADO)
        self.assertNotEqual(j.returncode, 0)
        self.assertLess(time.time() - t0, 10.0)

    def test_encerramento_inesperado(self):
        j = self.make_job(frames=(1, 5, 1), behavior={"mode": "crash"})
        j.start()
        wait(j)
        self.assertEqual(j.state, jobmod.ERRO)
        self.assertEqual(j.returncode, 139)
        self.assertIn("139", j.message)
        self.assertIn("Segmentation fault", j.message)
        self.assertFalse(os.path.exists(j.blend_path))

    def test_erro_python_no_worker(self):
        j = self.make_job(scene="CenaInexistente")
        j.start()
        wait(j)
        self.assertEqual(j.state, jobmod.ERRO)
        self.assertEqual(j.returncode, 1)
        self.assertIn("CenaInexistente", j.message)

    def test_operador_cancelado(self):
        j = self.make_job(behavior={"mode": "fail_op"})
        j.start()
        wait(j)
        self.assertEqual(j.state, jobmod.ERRO)
        self.assertIn("câmera", j.message)

    def test_nao_sobrescreve_sem_autorizacao(self):
        os.makedirs(self.out_dir)
        existing = os.path.join(self.out_dir, "frame_0002.png")
        with open(existing, "w") as fh:
            fh.write("ORIGINAL")
        j = self.make_job(frames=(1, 3, 1), overwrite=False)
        j.start()
        wait(j)
        self.assertEqual(j.state, jobmod.CONCLUIDO, j.message)
        with open(existing) as fh:
            self.assertEqual(fh.read(), "ORIGINAL")

    def test_sobrescreve_com_autorizacao(self):
        os.makedirs(self.out_dir)
        existing = os.path.join(self.out_dir, "frame_0002.png")
        with open(existing, "w") as fh:
            fh.write("ORIGINAL")
        j = self.make_job(frames=(1, 3, 1), overwrite=True)
        j.start()
        wait(j)
        with open(existing) as fh:
            self.assertEqual(fh.read(), "frame 2\n")

    def test_gpu_indisponivel_cai_para_cpu(self):
        j = self.make_job(device="GPU", gpu_available=False)
        j.start()
        wait(j)
        self.assertEqual(j.state, jobmod.CONCLUIDO, j.message)
        self.assertTrue(any("CPU" in w for w in j.worker_status.get("warnings", [])))

    def test_gpu_detectada(self):
        j = self.make_job(device="GPU", gpu_available=True)
        j.start()
        wait(j)
        self.assertIn("Fake GPU (CUDA)", j.worker_status.get("devices", []))
        self.assertEqual(j.worker_status.get("warnings"), [])

    def test_forcar_cpu(self):
        j = self.make_job(device="GPU", gpu_available=True, extra_config={"device_mode": "CPU"})
        j.start()
        wait(j)
        with open(j.log_path, encoding="utf-8") as fh:
            self.assertIn("Dispositivo forçado: CPU", fh.read())

    def log_text(self, j):
        with open(j.log_path, encoding="utf-8") as fh:
            return fh.read()

    def test_modo_rapido_usa_gpu_e_dados_persistentes(self):
        j = self.make_job(device="CPU", gpu_available=True,
                          extra_config={"speed_mode": "RAPIDO"})
        j.start()
        wait(j)
        self.assertEqual(j.state, jobmod.CONCLUIDO, j.message)
        log = self.log_text(j)
        self.assertIn("usando a GPU nesta renderização", log)
        self.assertIn("dados persistentes", j.worker_status["speed"])
        self.assertIn("amostragem adaptativa", j.worker_status["speed"])
        self.assertFalse(any(x.startswith("amostras") for x in j.worker_status["speed"]),
                         "o modo Rápido não pode reduzir a qualidade")

    def test_modo_rapido_ativa_gpu_desligada_nas_preferencias(self):
        j = self.make_job(device="CPU", gpu_available="present",
                          extra_config={"speed_mode": "RAPIDO"})
        j.start()
        wait(j)
        self.assertEqual(j.state, jobmod.CONCLUIDO, j.message)
        self.assertIn("GPU ativada automaticamente: CUDA", self.log_text(j))
        self.assertIn("Fake GPU (CUDA)", j.worker_status["devices"])

    def test_modo_rapido_sem_gpu_continua_na_cpu(self):
        j = self.make_job(device="CPU", gpu_available=False,
                          extra_config={"speed_mode": "RAPIDO"})
        j.start()
        wait(j)
        self.assertEqual(j.state, jobmod.CONCLUIDO, j.message)
        self.assertNotIn("usando a GPU", self.log_text(j))

    def test_modo_rascunho_reduz_amostras(self):
        j = self.make_job(extra_config={"speed_mode": "RASCUNHO"})
        j.start()
        wait(j)
        self.assertEqual(j.state, jobmod.CONCLUIDO, j.message)
        speed = j.worker_status["speed"]
        for item in ("amostras<=64", "denoise", "rebatimentos<=6", "limiar de ruído 0.05"):
            self.assertIn(item, speed)

    def test_modo_rascunho_eevee(self):
        j = self.make_job(engine="BLENDER_EEVEE", extra_config={"speed_mode": "RASCUNHO"})
        j.start()
        wait(j)
        self.assertIn("amostras Eevee<=16", j.worker_status["speed"])

    def test_modo_equilibrado_nao_altera_a_cena(self):
        j = self.make_job(device="CPU", gpu_available=True)
        j.start()
        wait(j)
        self.assertEqual(j.worker_status["speed"], [])
        self.assertNotIn("usando a GPU", self.log_text(j))

    def test_para_se_o_blender_principal_morrer(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        j = self.make_job(frames=(1, 20, 1), behavior={"frame_time": 0.05})
        # Simula a morte do processo pai trocando o PID registrado.
        orig_write = jobmod.write_json_atomic

        def patched(path, data):
            if path == j.job_path:
                data = dict(data, parent_pid=dead.pid)
            orig_write(path, data)

        jobmod.write_json_atomic = patched
        try:
            j.start()
        finally:
            jobmod.write_json_atomic = orig_write
        wait(j)
        self.assertEqual(j.worker_status.get("state"), "cancelled")
        self.assertIn("fechado", j.worker_status.get("message", ""))
        self.assertEqual(self.outputs(), [])

    def test_comando_montado(self):
        j = self.make_job(threads=3, extra_args=["-y"])
        cmd = j.build_command()
        self.assertEqual(cmd[:2], FAKE_BLENDER)
        # -y precisa vir antes do .blend para valer no carregamento do arquivo.
        self.assertEqual(cmd[2:5], ["-b", "-y", j.blend_path])
        self.assertEqual(cmd[cmd.index("-t") + 1], "3")
        # --python-exit-code precisa vir antes de -P; o job.json vem após '--'.
        self.assertLess(cmd.index("--python-exit-code"), cmd.index("-P"))
        self.assertEqual(cmd[-2:], ["--", j.job_path])

    def test_executavel_inexistente(self):
        j = self.make_job()
        j.launcher = [os.path.join(self.dir, "nao_existe", "blender")]
        with self.assertRaises(jobmod.JobError):
            j.start()
        self.assertFalse(j.is_running)

    def test_manager_impede_renderizacoes_simultaneas(self):
        manager = jobmod.JobManager()
        j1 = self.make_job(frames=(1, 30, 1), behavior={"frame_time": 0.1})
        manager.start(j1)
        self.assertTrue(manager.is_running)
        j2 = self.make_job()
        with self.assertRaises(jobmod.JobError):
            manager.start(j2)
        self.assertIsNone(j2.process, "o segundo job não pode ter sido iniciado")
        manager.shutdown()
        self.assertFalse(manager.is_running)
        self.assertEqual(j1.state, jobmod.CANCELADO)
        # Depois de terminar, um novo job pode ser iniciado.
        j3 = self.make_job(frames=(1, 2, 1))
        manager.start(j3)
        wait(j3)
        self.assertEqual(j3.state, jobmod.CONCLUIDO, j3.message)

    def test_shutdown_mantem_processo_quando_configurado(self):
        manager = jobmod.JobManager()
        j = self.make_job(frames=(1, 4, 1), behavior={"frame_time": 0.1})
        manager.start(j)
        manager.shutdown(keep_running=True)
        self.assertIsNone(j.process.poll())
        wait(j)
        self.assertEqual(j.state, jobmod.CONCLUIDO)


class HelperTests(unittest.TestCase):
    def test_count_frames(self):
        self.assertEqual(jobmod.count_frames(1, 250, 1), 250)
        self.assertEqual(jobmod.count_frames(1, 10, 3), 4)
        self.assertEqual(jobmod.count_frames(5, 1, 1), 0)

    def test_parse_frame_fraction(self):
        lines = [
            "Fra:3 Mem:1M | Sample 10/100",
            "Fra:4 Mem:1M | Sample 50/200",
        ]
        self.assertAlmostEqual(jobmod.parse_frame_fraction(lines, 4), 0.25)
        self.assertAlmostEqual(jobmod.parse_frame_fraction(lines, 3), 0.10)
        self.assertEqual(jobmod.parse_frame_fraction(lines, 9), 0.0)
        eevee = ["Fra:7 Mem:5M | Scene | Rendering 16 / 64 samples"]
        self.assertAlmostEqual(jobmod.parse_frame_fraction(eevee, 7), 0.25)

    def test_read_log_tail_com_carriage_return(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as fh:
            fh.write(b"linha 1\nFra:1 | Sample 1/4\rFra:1 | Sample 2/4\r\n\xff invalido\n")
            path = fh.name
        try:
            lines = jobmod.read_log_tail(path)
            self.assertIn("Fra:1 | Sample 2/4", lines)
            self.assertAlmostEqual(jobmod.parse_frame_fraction(lines, 1), 0.5)
        finally:
            os.remove(path)
        self.assertEqual(jobmod.read_log_tail("/caminho/inexistente.log"), [])

    def test_read_json_invalido(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as fh:
            fh.write("{incompleto")
            path = fh.name
        try:
            self.assertIsNone(jobmod.read_json(path))
        finally:
            os.remove(path)

    def test_format_duration(self):
        self.assertEqual(jobmod.format_duration(5), "5s")
        self.assertEqual(jobmod.format_duration(125), "2m05s")
        self.assertEqual(jobmod.format_duration(3725), "1h02m05s")


class CompatibilityTests(unittest.TestCase):
    """Blender 3.6 embute Python 3.10: nenhuma sintaxe mais nova é permitida."""

    def test_sintaxe_python_310(self):
        for name in sorted(os.listdir(ADDON)):
            if name.endswith(".py"):
                with open(os.path.join(ADDON, name), encoding="utf-8") as fh:
                    ast.parse(fh.read(), filename=name, feature_version=(3, 10))

    def test_bl_info(self):
        with open(os.path.join(ADDON, "__init__.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in tree.body:
            if isinstance(node, ast.Assign) and node.targets[0].id == "bl_info":
                info = ast.literal_eval(node.value)
                break
        else:
            self.fail("bl_info ausente")
        self.assertEqual(info["blender"], (3, 6, 0))
        self.assertEqual(info["category"], "Render")


if __name__ == "__main__":
    unittest.main()
