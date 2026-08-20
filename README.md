# Remux Dispatcher

Serviço container-first para avaliar, agendar e executar remuxes seletivos na stack
Radarr/Sonarr/Plex. O projeto está sendo implementado por fases conforme `PLAN.md`.

Estado atual: fases 0 a 8 concluídas e rollout inicial da Fase 9 ativo. Unmanic está
parado no profile `legacy`; hooks, execução imediata, agenda e cota do dispatcher estão
ativos. A remoção definitiva do legado aguarda o primeiro ciclo completo agendado.

## Desenvolvimento

Requer Python 3.12.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

O servidor requer `TRANSCODER_API_KEY` com pelo menos 16 caracteres. Paths e
estado usam defaults containerizados, mas podem ser substituídos em desenvolvimento.

```bash
TRANSCODER_API_KEY=your-key .venv/bin/python -m app.main
```

Endpoints disponíveis nesta fase:

- `GET /api/v1/health`
- `GET /api/v1/status`
- `POST /api/v1/reports`
- `POST /api/v1/webhooks/radarr` (sem autenticação)
- `POST /api/v1/webhooks/sonarr` (sem autenticação)
- `GET /api/v1/media?state=`
- `GET /api/v1/search?q=`
- `POST /api/v1/manual-runs`
- `GET /api/v1/manual-runs/{id}`
- `POST /api/v1/jobs/{id}/cancel`

Todos exigem o header `X-API-Key`, exceto `POST /api/v1/webhooks/radarr` e `POST /api/v1/webhooks/sonarr`, que recebem os
payloads nativos do Radarr e Sonarr e só são alcançáveis pela rede interna.

## Gate da Fase 1

- configuração tipada e secrets omitidos de `repr`/logs/eventos;
- validação de roots, extensões, traversal, arquivos ocultos/parciais e symlinks;
- SQLite em WAL com migração atômica, foreign keys e claims/transições;
- API autenticada e health/status lidos do SQLite;
- 16 testes automatizados e compilação de todos os módulos concluídos.

## Gate da Fase 2

- fingerprint por path/tamanho/mtime e estabilidade persistida no SQLite;
- cache de probe/plano inclui fingerprint, assinatura da política e idioma original;
- scanner não segue symlinks e ignora ocultos, parciais e extensões não permitidas;
- política combinada cobre áudio, downmix, ordem e limite total de streams;
- geração de uma única argv sem shell, sem execução e sem normalizações SMA;
- preservação planejada de metadata, capítulos, attachments e disposições;
- 36 testes automatizados e cinco reports read-only sobre mídia real.

Resultados reais confirmados:

- `A Complete Unknown`: compatível, nenhuma argv;
- `Beast`: DTS identificado para EAC3;
- `House of the Dragon S01E02`: 31 → 6 streams contados, mantendo PT/EN;
- `Darker Than Black S02E10`: japonês movido para a primeira posição;
- `Ghost in the Shell: SAC S01E15`: FLAC + ordem, em um único plano.

## Gate da Fase 3

- executor aceita somente argv FFmpeg sem shell, reporta progresso e suporta
  cancelamento gracioso;
- validação compara duração, container, streams, codecs, canais, bitrate, HDR/DV,
  colorimetria, capítulos, attachments, metadata e disposições;
- espaço livre é verificado no cache e no volume final com margem;
- saída é copiada com buffer, fsync e validação novamente ao lado da origem;
- promoção MKV é atômica e mudança de extensão usa backup/marker recuperável;
- hardlink mantém o arquivo de torrent enquanto troca apenas o path da biblioteca;
- cache completo é preservado quando somente a promoção precisa ser retomada;
- 54 testes passaram, inclusive um ciclo FFmpeg real inteiramente temporário.

## Gate da Fase 4

- dias PT-BR/inglês e horários `HH:MM`, incluindo janela atravessando meia-noite;
- janela de 24h inteiras quando `WINDOW_START == WINDOW_END` (a âncora é a data de
  início); cota de jobs reinicia no giro da janela;
- estado adaptativo, janela, cota e scans persistidos em SQLite com instantes UTC;
- backlog impede novo scan; drenagem agenda confirmação para a janela seguinte;
- scan vazio aplica cooldown por dias de calendário e missed-run espera nova janela;
- scan em andamento é persistido e recuperado após interrupção/restart;
- manual/import/upgrade passam fora da janela e não consomem cota;
- jobs de scan/retry respeitam janela e cota; `0` permanece ilimitado;
- status da API expõe decisão, janela, uso, backlog e próxima execução;
- 68 testes automatizados passaram.

## Gates das fases 5 e 6

- eventos Arr são persistidos com outbox na mesma transação e redelivery idempotente;
- contratos `RefreshMovie`/`RenameMovie`, `RescanSeries`/`RenameFiles` são exatos e o
  Sonarr renomeia somente o arquivo processado;
- endpoint único `POST /api/v1/webhooks/arr` consome o payload nativo do Radarr/Sonarr
  (Download/Upgrade/Rename/Delete) sem autenticação e normaliza para os eventos internos;
- sidecars SRT/ASS/SSA/VTT/SUB/SUP/IDX preservam sufixos e pares, sem sobrescrever
  colisões; Bazarr recebe `scan-disk` depois da convergência;
- delete gerado pela migração para MKV preserva sidecars e estado;
- Plex bloqueia promoção do arquivo em reprodução e falha fechado se indisponível;
- refresh do Plex é direcionado à pasta da seção correspondente;
- Dockerfile inclui FFmpeg e o Compose expõe agenda, cota e política diretamente;
- 94 testes passaram; Compose, build e smoke test efêmero de healthcheck passaram.

## Gate da Fase 7

Os cinco negativos e quatro positivos foram confirmados em mounts read-only. A matriz,
os caminhos exatos e fingerprints estão em `PHASE7_REPORT.md`. Nenhuma origem não-MKV
atual requer ação, portanto esse cenário continuará restrito a fixture efêmera.

## Gate da Fase 8 e rollout inicial

Quatro arquivos reais passaram de ponta a ponta, cobrindo ordem, limite de streams,
transcode, critérios combinados e preservação HDR/Dolby Vision. O hook real confirmou
que Alien Covenant permanece compatível sem plano/FFmpeg. Detalhes e correções feitas
durante o gate estão em `PHASE8_REPORT.md`.

O Compose padrão sobe `transcoder`, não Unmanic. A agenda está ativa para todos
os dias com janela de processamento 24h (`00:00`–`00:00`), cota ilimitada e cooldown
7 dias. A tolerância de duração da validação é configurável via
`DURATION_TOLERANCE_SECONDS` (default 2.0; no Compose, 5.0) para absorver o artefato
de muxer em que mkvmerge reporta o container alguns segundos mais longo que o stream
mais longo, sem perda de conteúdo. A validação também ignora artefatos benignos de
muxer que o FFmpeg não reproduz no remux: tags de estatísticas do mkvmerge
(`BPS`, `NUMBER_OF_*`, `_STATISTICS_*`), `creation_time`, `Writing frontend` do StaxRip,
prefixos `Writing*`/`Encoding*` (ex.: `Writing application`/`Writing library` do IFME),
a normalização de `codec_tag` e a reordenação/case das chaves (a comparação usa
chaves em minúsculas e ordenadas) — enquanto continua comparando codec, profile,
pixel format, colorimetria, HDR/DV e streams. Radarr/Sonarr usam uma única conexão
`Webhook` para Download/Upgrade/Rename/Delete apontando para `http://transcoder:8100/api/v1/webhooks/arr`.

A cada varredura o banco é reconciliado com o disco: arquivos rastreados que
sumiram são marcados `deleted` (cancelando planos ativos), linhas `deleted`
confirmadas ausentes há mais de `RECONCILE_DELETED_GRACE_HOURS` (default 24h) são
removidas em cascata, e por arquivo só fica o plano terminal mais recente (estado
atual). Se a varredura enxergar menos da metade dos arquivos rastreados (ex.: raiz
desmontada), a reconciliação é pulada por segurança; arquivos que voltam ao disco
são revividos automaticamente.
