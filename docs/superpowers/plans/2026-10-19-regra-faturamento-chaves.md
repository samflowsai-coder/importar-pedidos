# Regra de faturamento — evidência da Task 0 (Fire de produção, só leitura, 20/10/2026)

Leitura por TCP, `READ_COMMITED_RO`, só SELECT, nas duas bases:
`mm` = 192.168.15.7 `MM_AMERICANENSE.FDB` · `nasmar` = 192.168.15.4 `MM_CONFECCAO.FDB`.

## Coluna da UF

`CADASTRO.UF` (siglas; há também `UFCOB` e `UFENT`, de cobrança e entrega — a regra usa `UF`, a do CNPJ faturado).

| base | clientes (`RELAC_CLIENTE='Sim'`) por UF | sem UF | mesmo CPF_CNPJ com UFs diferentes |
|---|---|---|---|
| nasmar | SP 25.369 · MG 807 · RJ 684 · PR 363 · RS 321 · … | 1 | 0 |
| mm | SP 573 · RS 12 · SC 11 · MG 11 · RJ 10 · PR 9 · … · `EX` 4 | 4 | 0 |

`EX` (exterior) existe na mm: pela regra vira "fora de SP".

## Chaves das redes fixas

Nenhuma das quatro existe na base nasmar (0 linhas por raiz). Todas estão na mm.

| rede | tipo | chave | evidência na mm |
|---|---|---|---|
| Centauro | raiz | `06347409` | 1 cadastro: 06.347.409/0296-51 SBF COMERCIO (SP), 959 pedidos |
| Riachuelo | raiz | `33200056` | 3 cadastros LOJAS RIACHUELO: /0313-70 (AM), /0343-96 (RN), /0360-97 (SP) |
| Kallan | raiz | `51540219` | 35 cadastros KALLAN CALCADOS (33 SP, 1 BA, 1 SE) |
| Sam's Club | cnpj | os 12 CDs WMB abaixo | razão WMB SUPERMERCADOS DO BRASIL (raiz 00063960) |

Sam's (WMB), por CNPJ completo (spec §10.8): 00063960000109 (SP), 00063960004848 (BA), 00063960006131 (BA),
00063960006999 (PE), 00063960009408 (RS), 00063960012298 (SP), 00063960057984 (MG), 00063960058018 (SP),
00063960058794 (DF), 00063960058875 (PR), 00063960058956 (RJ), 00063960059170 (RS).

Riachuelo e Kallan têm cadastros fora de SP: sem a rede fixa, a regra por UF mandaria esses para a Nasmar. É o caso que a lista da Bianca resolve.
