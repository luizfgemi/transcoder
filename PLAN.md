# PLANO — Refatoração do Transcoder (DOVI, contratos, Bazarr, webhooks por app)

## Contexto

O transcoder (Python/FastAPI em container) avalia arquivos de mídia e decide
transcodificar (HEVC NVENC), copiar ou pular, com validação pós-processamento e
fila com janela de execução. Quatro problemas em produção:

1. **Bug de semântica DOVI**: NVENC não preserva DOVI, mas `validate_output`
   exigia DOVI no output (`app/engine.py:207-209`) → jobs DOVI falhavam em loop.
   Correção preliminar errada (`has_dovi` em `app/policy.py:194-201`) a reverter.
2. **Bazarr 405**: `BAZARR_URL=http://bazarr:6767/api` + código concatena
   `/api/system/tasks` → `/api/api/...` → 405.
3. **Integração Arr quebrada**: Radarr tem Webhook "MediaHub Lite" (serviço morto)
   + CustomScript "Transcoder" → `/custom-scripts/transcoder-arr-hook.sh`
   (**inexistente**, log cheio de `No such file or directory`); Sonarr só tem o
   Webhook "MediaHub Lite" (morto).
4. **Parser de webhook com campos errados**: `renamedFiles` não existe no payload
   real; duplicações (`get_cached_evaluation`, `create_app`/`build_app`); ausência
   de contratos por função/arquivo.

## Decisões (confirmadas)

- **DOVI**: remover sempre (a TV do operador transcoda Dolby Vision; saída HEVC
  HDR10 sem DOVI).
- **Escopo**: Fases 1+2 (DOVI + contratos/duplicação), Bazarr 405, integração webhook.
- **Integração**: **um endpoint por app** — `/api/v1/webhooks/radarr` e
  `/api/v1/webhooks/sonarr` (formato divergente confirmado; facilita manutenção).
  Bazarr não envia webhook (o transcoder chama o Bazarr) — sem endpoint para ele.
- **Contratos**: docstring em **todas as funções públicas** + **cabeçalho de
  contrato no topo de cada arquivo** em `app/`.

---

## Fase 1 — Correção DOVI

**1.1 `app/media.py` — `Stream.has_dovi`**

- Propriedade derivada em `Stream` (linha ~96):
  `any("dovi" in s.lower() for s in self.side_data_types)`.

**1.2 `app/policy.py` — DOVI força transcode**

- Reverter o `has_dovi` errado (linhas 194-201); DOVI passa a ser critério próprio
  de transcode (independente de bitrate/codec).
- **Bump `RULESET_VERSION`** (linha 147) de `3` para `4` — invalida o cache de
  avaliação persistido (a assinatura do plano inclui `rulesetVersion`).

**1.3 `app/engine.py` — `validate_output` (inverter checagem DOVI)**

- Linhas 207-209 hoje exigem que o output **mantenha** DOVI quando a fonte tem DOVI.
- Novo contrato:
  - `transcoded` → DOVI **ausente** obrigatório (`ValidationError("DOVI
    configuration record still present")`).
  - `copied` → DOVI **presente** obrigatório (`ValidationError("DOVI
    configuration record missing")`).

**1.4 Testes**

- `tests/test_policy.py:221`: substituir
  `test_high_bitrate_dovi_video_is_not_transcoded` por:
  - `test_dovi_video_is_transcoded_regardless_of_bitrate`
  - `test_dovi_low_and_high_bitrate_both_transcoded` (parametrizado)
- `tests/test_validation.py`: adicionar:
  - `test_transcoded_output_must_not_contain_dovi`
  - `test_copied_output_must_retain_dovi`

---

## Fase 2 — Webhooks por app + parser correto

**2.1 `app/api.py` — dois endpoints**

- Substituir `POST /api/v1/webhooks/arr` (linhas 197-201) por:
  - `POST /api/v1/webhooks/radarr` → `normalize_radarr_webhook`
  - `POST /api/v1/webhooks/sonarr` → `normalize_sonarr_webhook`
- Ambos retornam `202` com `{"status":"accepted","events":[...]}`.

**2.2 `app/integrations.py` — parsers por app**

- Dividir `normalize_arr_webhook` (linhas 579-674) em dois com schema fixo, com
  base nos payloads reais (Radarr 6.3 / Sonarr 4.0, serialização camelCase,
  `eventType` PascalCase):

**Radarr** (`movie`):

- `Download`/`Upgrade` → `movie` + `movieFile` (singular) → import;
  `isUpgrade` só informa.
- `Rename` → `renamedMovieFiles[]` (com `previousPath`, `path`) → rename.
- `MovieFileDelete` → `movieFile` → delete.
- `Test` → [].

**Sonarr** (`series`):

- `Download` → `episodeFile` (singular) **ou** `episodeFiles[]`
  (ImportComplete) → import.
- `Rename` → `renamedEpisodeFiles[]` (com `previousPath`, `path`) → rename.
- `EpisodeFileDelete` → `episodeFile` → delete.
- `Test` → [].

**2.3 Testes `tests/test_arr_hook.py` (reescrever)**

- Payloads reais copiados dos `Webhook*Payload.cs` (Radarr 6.3, Sonarr 4.0):
  `renamedMovieFiles`/`renamedEpisodeFiles`, `episodeFiles` lista,
  `MovieFileDelete` + `EpisodeFileDelete`, `Test`, erros de payload inválido.
- Cobrir Radarr e Sonarr separadamente.

---

## Fase 3 — Contratos (docstrings + cabeçalhos)

Em **todos os arquivos de `app/`**, adicionar:

- **Cabeçalho de contrato** no topo: responsabilidade, entradas/saídas
  principais, dependências, invariantes.
- **Docstring em toda função pública** (e helpers não triviais): propósito,
  args, retorno, exceções, contrato.

Arquivos e contratos a documentar:

- `media.py` — `MediaProbe`/`Stream`/`FFprobeRunner`: campos que policy/engine
  dependem (codec, bitrate, side_data, color transfer/primaries/space), `has_dovi`.
- `policy.py` — `Policy.evaluate → RemuxPlan`; regras de decisão
  (DOVI/codec/bitrate); `RULESET_VERSION` = assinatura de cache.
- `engine.py` — `validate_output` (invariantes DOVI/HDR/streams/fingerprint);
  `ProcessingPipeline`, `FFmpegExecutor`, `SafePromoter`.
- `database.py` — tabelas, chaves de cache (`policy_signature`), transições de
  estado (espelhar `ALLOWED_TRANSITIONS`), `get_cached_evaluation` único.
- `domain.py` — enums e `ALLOWED_TRANSITIONS`.
- `daemon.py` — fila, janela, retry (re-executa plano persistido), reconcile.
- `integrations.py` — parsers por app, `ArrPostProcessor`, `ArrPathMapper`,
  `ArrClient`s, fluxo Arr→Bazarr.
- `api.py` — rotas por app, auth (`X-API-Key` exceto webhooks), fábrica de app única.
- `config.py` — variáveis de ambiente, defaults, validação.
- `main.py` — bootstrap, montagem de dependências.

**Remover duplicações** (dentro desta fase):

- `database.py`: `get_cached_evaluation` duplicado (linhas ~316 e ~721) → manter um.
- `api.py`: `create_app`/`build_app` (linhas 56/286) → unificar em uma fábrica.

---

## Fase 4 — Correção Bazarr 405

**4.1 `media/docker-compose.yml:331`**

- `BAZARR_URL=http://bazarr:6767/api` → `BAZARR_URL=http://bazarr:6767`.
- O código já concatena o sufixo `/api/...`; os testes já usam `base_url` sem
  sufixo. Sem mudança de código.

---

## Fase 5 — Configuração webhook no Radarr/Sonarr

**Radarr** (via API `PUT /api/v3/notification`):

- Deletar "MediaHub Lite" (id 7) e CustomScript "Transcoder" (id 8, script
  inexistente).
- Criar **um Webhook**: URL `http://transcoder:8100/api/v1/webhooks/radarr`,
  POST; onDownload, onUpgrade, onRename, onMovieFileDelete
  (+ `onMovieFileDeleteForUpgrade` — decidir).

**Sonarr** (idem):

- Deletar "MediaHub Lite".
- Criar Webhook: `http://transcoder:8100/api/v1/webhooks/sonarr`, POST;
  onDownload, onUpgrade, onRename, onEpisodeFileDelete.

Validação: botão Test em cada um + conferir no log do transcoder que o evento
`Test` chega e retorna `accepted`; depois um download/rename real.

---

## Fase 6 — Deploy e validação

1. `docker compose --env-file media/.env -f media/docker-compose.yml config --quiet`.
2. Rebuild + restart do transcoder (`up -d --build transcoder`) — requer aprovação.
3. Logs: 405 sumiu; DOVI transcoda e valida; webhooks chegando; cache v3 invalidado.
4. `pytest` verde em `repos/transcoder`.

## Ordem de execução

Fase 1 → Fase 2 → Fase 3 → `pytest` → Fase 4 → Fase 5 (com aprovação) →
Fase 6 (deploy, com aprovação).

## Decisões pendentes

- **Upgrade**: o parser mantém o tratamento de `eventType: "Upgrade"` como tipo
  próprio (mesmo não existindo no real), ou remove e trata tudo como `Download`
  com `isUpgrade`?
- **Radarr `onMovieFileDeleteForUpgrade`**: ativar ou não na config webhook?