"""Roteamento de pedido para ambiente (multi-empresa).

Quem decide em que empresa um pedido entra é o **documento**, não um cadastro
de clientes — ver `docs/superpowers/specs/2026-08-24-roteamento-intercompany-nasmar-design.md`,
Revisão 4. Este pacote é a escada dessa decisão e nada mais: não abre conexão
por conta própria fora de `historico.py`, não escreve em lugar nenhum, e nunca
devolve um ambiente default.
"""
