# AutoRender Background (Blender 3.6.22)

Add-on que renderiza a animação do projeto em **um processo separado do Blender**
com um único botão, deixando a interface principal livre para continuar
editando ou reproduzindo a timeline.

> **Importante:** reproduzir a timeline e renderizar ao mesmo tempo **não é
> garantido**. Os dois processos disputam CPU, GPU, memória e disco; o FPS da
> reprodução pode cair bastante, principalmente com Eevee ou Cycles na GPU.

## Estrutura

```
auto_render_background/      <- pasta do add-on (é isto que vai no .zip)
├── __init__.py              bl_info, register/unregister
├── preferences.py           preferências do add-on
├── operators.py             botões (renderizar, cancelar, abrir pasta, log, GPU) e timer
├── ui.py                    painel AUTO RENDER (View 3D e Sequencer, barra lateral N)
├── scene_info.py            leitura das configurações e validações (usa bpy)
├── job.py                   gerenciamento do subprocess e IPC (não usa bpy)
└── worker.py                script executado dentro do Blender em segundo plano
tests/                       testes automatizados (Blender simulado)
tools/build_zip.py           gera o .zip de instalação
```

## Instalação

1. Gere o pacote: `python tools/build_zip.py` → `dist/auto_render_background.zip`
   (ou compacte manualmente a pasta `auto_render_background/`, mantendo a pasta
   dentro do zip).
2. No Blender 3.6.22: **Edit > Preferences > Add-ons > Install...**, selecione o `.zip`.
3. Ative **Render: AutoRender Background**.
4. (Opcional) Ajuste as preferências do add-on, descritas abaixo.

Instalação manual: copie a pasta `auto_render_background` para
`<config do Blender>/3.6/scripts/addons/` e ative o add-on.

## Utilização

1. Configure a cena normalmente: motor (Cycles/Eevee), resolução, FPS,
   intervalo de frames, formato e **caminho de saída** (Propriedades de Saída).
2. **Salve o projeto** pelo menos uma vez (o add-on recusa arquivos nunca salvos).
3. Abra a barra lateral **N** na Viewport 3D (ou no Video Sequencer) → aba
   **Auto Render**.
4. Clique em **RENDERIZAR AGORA**.
   - Se já existirem arquivos de saída, uma janela pede autorização para
     sobrescrever. Sem autorização a renderização não começa.
   - Se houver alterações não salvas, você escolhe entre salvar o `.blend` ou
     renderizar o estado atual a partir de uma cópia (o original não é tocado).
5. Acompanhe **STATUS** (Aguardando, Renderizando, Concluído, Erro, Cancelado),
   **PROGRESSO** (frames concluídos + amostras do frame atual lidas do log) e o
   tempo decorrido. Você pode continuar usando o Blender e dar play na timeline.
6. **CANCELAR** pede o encerramento seguro: o processo termina logo após gravar
   o frame atual. Se não responder dentro do tempo configurado, é encerrado à força.
7. **ABRIR PASTA** abre o diretório de saída. Em caso de erro, **Abrir log**
   mostra a saída completa do processo de renderização.

### Preferências

| Opção | Padrão | Efeito |
|---|---|---|
| Velocidade | Rápido | Ver "Modos de velocidade" abaixo (também aparece no painel) |
| Threads de CPU | 0 (auto) | Passa `-t N`; deixar núcleos livres ajuda a reprodução |
| Dispositivo (Cycles) | Igual à cena | "Forçar CPU" deixa a GPU para a viewport |
| Cópia do projeto | Pasta do .blend | Onde fica a cópia temporária usada pelo processo |
| Espera para cancelar | 10 s | Tempo aguardando o fim do frame antes de encerrar à força |
| Continuar ao fechar o Blender | desligado | Se desligado, o render para quando o Blender principal fecha |
| Executável do Blender | vazio | Vazio = mesmo executável da sessão (`bpy.app.binary_path`) |

### Modos de velocidade

As otimizações são aplicadas **somente na cópia** que está sendo renderizada; o
projeto original não é alterado.

| Modo | O que faz | Qualidade |
|---|---|---|
| **Rápido** (padrão) | Usa a GPU automaticamente (mesmo que a cena esteja em CPU ou a GPU esteja desligada nas Preferências — escolhe OptiX > CUDA > HIP > Metal > oneAPI), ativa dados persistentes (reaproveita BVH/texturas entre frames) e amostragem adaptativa, prioridade normal do processo | Igual |
| **Rascunho** | Tudo do Rápido + no máximo 64 amostras com denoise, limiar de ruído 0.05, menos rebatimentos de luz (Cycles) / no máximo 16 amostras (Eevee) | Menor |
| **Equilibrado** | Renderiza a cena exatamente como está, com prioridade baixa | Igual, mais lento; reprodução da timeline mais fluida |

Dados persistentes usam mais memória. Se a cena não couber na RAM/VRAM, use
o modo Equilibrado. O que foi aplicado aparece no log (`Velocidade (...)`).

## Como funciona

1. O botão valida: projeto salvo, câmera (ou faixas no Sequencer), motor
   suportado, intervalo de frames, diretório de saída (criado e testado com um
   arquivo temporário) e arquivos já existentes (via `RenderSettings.frame_path`).
2. O estado atual é gravado em uma **cópia** com
   `bpy.ops.wm.save_as_mainfile(copy=True)` — o `.blend` original não é alterado
   e você pode continuar editando sem afetar o render em andamento.
3. Um novo Blender é iniciado com `subprocess.Popen`:
   `blender -b copia.blend [-y|-Y] [-t N] --python-exit-code 1 -P worker.py -- job.json`
   (stdout/stderr vão para `render.log`, nunca para um pipe que possa travar).
4. `worker.py` aplica cena, frames, saída absoluta e permissão de sobrescrita,
   detecta as GPUs do Cycles **no próprio processo** (`refresh_devices()`), cai
   para CPU se a cena pede GPU e nenhuma estiver ativa, e chama
   `bpy.ops.render.render(animation=True)`.
5. Handlers `render_pre/post/write` gravam `status.json` (escrita atômica) e
   verificam `cancel.flag` entre frames. O Blender principal lê esses arquivos com
   `bpy.app.timers` a cada 0,5 s — sem threads, sem bloquear a interface.
6. Ao terminar (sucesso, erro ou cancelamento) a cópia temporária é apagada. O
   log fica na pasta temporária do job até a próxima renderização.

## Testes

```
python -m unittest discover -s tests -v
```

Os testes rodam sem Blender: `tests/fake_blender.py` aceita os mesmos
argumentos de linha de comando e executa o **`worker.py` real** com um `bpy`
simulado (`tests/fake_bpy.py`), através do mesmo `subprocess` usado pelo add-on.
Cobrem: renderização completa, passo de frames, progresso parcial e monotônico,
cancelamento seguro entre frames, cancelamento forçado de processo travado,
crash (código 139), exceção Python no worker, operador cancelado, não
sobrescrever sem autorização, sobrescrever com autorização, fallback GPU→CPU,
forçar CPU, parada quando o Blender principal morre, bloqueio de renderizações
simultâneas, registro/desregistro do add-on, validações e o fluxo do botão
(incluindo a garantia de que o `.blend` original não é modificado) e
compatibilidade de sintaxe com Python 3.10 (versão embutida no Blender 3.6).

### Teste manual no Blender 3.6.22 (recomendado)

1. Projeto simples (cubo animado), Cycles, 1–20, saída `//render/frame_####`, salvar.
2. RENDERIZAR AGORA → status "Renderizando", barra avançando, frames aparecendo
   em `render/`. Dê play na timeline durante o render.
3. Clique de novo no botão → deve estar desabilitado.
4. CANCELAR no meio → status "Cancelado"; frames já gravados estão íntegros.
5. Renderize de novo → janela pedindo autorização para sobrescrever.
6. Remova a câmera → mensagem de erro, nada é iniciado.
7. Troque para Eevee e para saída FFmpeg (MP4) e repita.
8. Em Edit > Preferences > System, ative uma GPU e confira a linha "GPU:" no
   painel e a linha "Dispositivos ativos" no log.

## Limitações conhecidas

- **Simultaneidade não garantida.** O processo secundário usa os mesmos recursos.
  Eevee sempre usa a GPU; Cycles na GPU disputa a mesma placa com a viewport.
  Use "Prioridade baixa", "Threads de CPU" e "Forçar CPU" para favorecer a reprodução.
- **Memória:** a cena é carregada duas vezes (Blender principal + processo de
  renderização). Cenas grandes podem esgotar RAM/VRAM.
- **Eevee em segundo plano** precisa de contexto OpenGL. Em servidores Linux sem
  display (headless) o Eevee do 3.6 falha; em desktops funciona normalmente.
- **Cancelar vídeo (FFmpeg):** o processo para entre frames, mas o arquivo de
  vídeo fica incompleto/possivelmente ilegível. Sequências de imagens são
  canceladas de forma limpa.
- **Progresso dentro do frame** vem do log (`Sample x/y` do Cycles,
  `Rendering x / y samples` do Eevee). Composição e gravação não têm progresso
  detalhado; nesses momentos a barra só avança ao concluir o frame.
- O cancelamento seguro e a parada por "Blender principal fechado" só acontecem
  **entre frames**; frames muito longos usam o encerramento forçado após o tempo
  de espera configurado. Se o Blender principal travar/crashar, o worker percebe
  apenas no início/fim do próximo frame.
- A proteção contra renderizações múltiplas vale por sessão do Blender; duas
  instâncias diferentes do Blender não se enxergam.
- Saída multiview (estéreo) não é considerada na verificação de arquivos existentes.
- Recarregar o add-on (F8/desativar) encerra a renderização em andamento, a
  menos que "Continuar ao fechar o Blender" esteja ativo (nesse caso o processo
  segue, mas o painel perde o controle sobre ele).
- O add-on foi validado com testes automatizados usando um Blender simulado;
  execute o roteiro de teste manual acima no Blender 3.6.22 real antes de usar
  em produção.
