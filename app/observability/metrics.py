"""Prometheus metrics for the Portal de Pedidos.

Metrics defined here:
- portal_outbox_pending_total   (Gauge)   — pending outbox rows
- portal_outbox_dead_total      (Gauge)   — dead-letter outbox rows
- portal_poll_fire_duration_seconds (Histogram) — Firebird poll job duration
- portal_webhook_received_total (Counter) — inbound webhooks by provider
- portal_reconcile_fire_last_run_ok (Gauge) — 1/0 por ambiente, última corrida
  da reconciliação com o Fire (`app.reconcile.runner.reconciliar`)

Usage
-----
Counters and Histograms are incremented/observed at event time (in their
respective handlers / jobs). Gauges are refreshed by calling
``update_outbox_metrics()`` — the drain_outbox job does this every 15 s.
``portal_reconcile_fire_last_run_ok`` é setado direto por `reconciliar()` ao
fim de cada corrida de verdade (não quando pulada por trava/lock) — é o
único jeito de responder "o job das 12h rodou?" num servidor Windows sem
console.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

outbox_pending_count: Gauge = Gauge(
    "portal_outbox_pending_total",
    "Pending outbox rows awaiting delivery to Gestor de Produção",
)

outbox_dead_count: Gauge = Gauge(
    "portal_outbox_dead_total",
    "Dead-letter outbox rows that exhausted all retry attempts",
)

poll_fire_duration_seconds: Histogram = Histogram(
    "portal_poll_fire_duration_seconds",
    "End-to-end duration of the Firebird status-poll job",
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

webhook_received_total: Counter = Counter(
    "portal_webhook_received_total",
    "Inbound webhook requests received",
    ["provider"],
)


price_check_acks_total: Counter = Counter(
    "portal_price_check_acks_total",
    "Total de ACKs do operador para itens sem preço cadastrado no Fire",
)

price_check_blocks_total: Counter = Counter(
    "portal_price_check_blocks_total",
    "Total de envios/exports bloqueados por validação de preço",
    labelnames=("reason",),  # price_mismatch | missing_order_price | no_price_unacked | preco_*
)

reconcile_fire_last_run_ok: Gauge = Gauge(
    "portal_reconcile_fire_last_run_ok",
    "1 se a última reconciliação com o Fire deste ambiente terminou sem "
    "erro_conexao, 0 caso contrário. Só atualizado quando a corrida roda de "
    "verdade (não quando pulada por trava ou por já estar em execução).",
    ["environment"],
)

cadastro_volta_total: Counter = Counter(
    "portal_flowpcp_cadastro_volta_total",
    "Cadastros da fila do Flow tratados pela volta, por resultado "
    "(simulado | erro_definitivo | ja_no_livro | criado | atualizado | inativado | "
    "adotado | liberado | incerto | confirmado | erro_registrado | transitorio | orfao | "
    "superado | repetir | bug_corpo | credencial).",
    ["env", "resultado"],
)
cadastro_volta_pendentes: Gauge = Gauge(
    "portal_flowpcp_cadastro_volta_pendentes",
    "Cadastros lidos da fila do Flow na última leitura da volta (no máximo 50; "
    "em simular a fila não anda, então não é o tamanho real da fila).",
    ["env"],
)
cadastro_volta_mais_antigo: Gauge = Gauge(
    "portal_flowpcp_cadastro_volta_mais_antigo_segundos",
    "Idade (aprovadoEm) do cadastro mais antigo da fila na última leitura.",
    ["env"],
)
cadastro_volta_sem_volta: Gauge = Gauge(
    "portal_flowpcp_cadastro_volta_sem_volta",
    "Linhas do livro com resultado (gravado no Fire ou erro) que o Flow ainda não "
    "aceitou (gravado_sem_volta + erro_a_enviar).",
    ["env"],
)
cadastro_volta_aguardando_modo: Gauge = Gauge(
    "portal_flowpcp_cadastro_volta_aguardando_modo",
    "Itens aprovados no Flow que o modo da volta não grava (contagem aproximada, "
    "no máximo 50, lida sem reservar). Sem série quando o Flow não respondeu.",
    ["env"],
)


def update_outbox_metrics() -> None:
    """Query todas as DBs de ambiente e atualiza outbox Gauges (soma global).

    Multi-ambiente: itera `router.list_env_slugs()` e agrega contadores
    de outbox de cada DB. Métrica é global; rotular por ambiente fica para
    quando virar requisito real.
    """
    from app.persistence import router  # noqa: PLC0415

    pending_total = 0
    dead_total = 0
    for slug in router.list_env_slugs():
        with router.env_connect(slug) as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status IN ('pending', 'retido') THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'dead'    THEN 1 ELSE 0 END)
                FROM outbox
                """
            ).fetchone()
        pending_total += row[0] or 0
        dead_total += row[1] or 0

    outbox_pending_count.set(pending_total)
    outbox_dead_count.set(dead_total)
