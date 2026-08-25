#!/usr/bin/env python3
"""Gera o PDF de validacao comercial do roteamento intercompany Nasmar -> MM.

Destinatario: Rafael. O documento existe para ele confirmar tres decisoes
comerciais antes de qualquer linha ser escrita no ERP. Cada pergunta vem com o
numero medido na base real, para a resposta ser informada e nao intuitiva.

Fontes: core do PDF (Times para display, Helvetica para texto). Sem TTF
embutida — fonte de sistema em documento que sai da empresa tem questao de
licenca, e latin-1 ja cobre a acentuacao do portugues inteira.

Uso: .venv/bin/python tools/generate_validacao_rafael_pdf.py
Saida: output/validacao-regras-nasmar-mm.pdf
"""

from __future__ import annotations

from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

OUTPUT = Path(__file__).parent.parent / "output" / "validacao-regras-nasmar-mm.pdf"

# Paleta: quase-preto para texto, azul profundo de acento, cinzas quentes.
TINTA = (24, 24, 27)
SUAVE = (110, 110, 118)
ACENTO = (19, 58, 112)
DESTAQUE = (146, 38, 38)
REGUA = (219, 217, 213)
FUNDO_BOX = (247, 246, 243)
FUNDO_ACENTO = (237, 242, 249)

# Latin-1 cobre toda a acentuacao do portugues. Só simbolos tipograficos
# precisam de substituto.
_SUBS = [
    # CUIDADO: en-dash (U+2013) tambem esta FORA do latin-1. Trocar travessao
    # por ele so troca um "?" por outro. Hifen simples e o unico traco seguro.
    # A forma com espacos vem PRIMEIRO: senao " — " vira "  -  " (espaco duplo).
    (" — ", " - "),
    ("—", " - "),
    ("–", "-"),
    ("→", " > "),
    ("≤", "<="),
    ("≥", ">="),
    ("•", "·"),
    ("’", "'"),
    ("‘", "'"),
    ("“", '"'),
    ("”", '"'),
    ("÷", "/"),
    ("×", "x"),
    ("≠", "!="),
    ("≈", "~"),
    (" ", " "),
]


def s(t: str) -> str:
    for a, b in _SUBS:
        t = t.replace(a, b)
    return t.encode("latin-1", "replace").decode("latin-1")


class Doc(FPDF):
    def multi_cell(self, w, h=None, text="", **kw):  # type: ignore[override]
        """O default do fpdf2 deixa o cursor na margem DIREITA (XPos.RIGHT), e a
        chamada seguinte fica com largura zero -> "Not enough horizontal space".
        Aqui todo bloco de texto e empilhado: voltar a esquerda e o certo em
        100% dos usos."""
        kw.setdefault("new_x", XPos.LMARGIN)
        kw.setdefault("new_y", YPos.NEXT)
        return super().multi_cell(w, h, text, **kw)

    @property
    def util(self) -> float:
        return self.w - self.l_margin - self.r_margin

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_y(11)
        self.set_font("Helvetica", size=7.5)
        self.set_text_color(*SUAVE)
        self.cell(
            0,
            4,
            s("Da Nasmar para a MM  ·  validação comercial"),
            align="L",
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )
        self.set_draw_color(*REGUA)
        self.set_line_width(0.2)
        y = self.get_y() + 1
        self.line(self.l_margin, y, self.w - self.r_margin, y)
        self.ln(7)

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font("Helvetica", size=7.5)
        self.set_text_color(*SUAVE)
        self.cell(0, 4, s(str(self.page_no())), align="C")

    # ── blocos ────────────────────────────────────────────────────────────

    def secao(self, sobretitulo: str, titulo: str) -> None:
        self.ln(4)
        y = self.get_y()
        self.set_draw_color(*ACENTO)
        self.set_line_width(1.4)
        self.line(self.l_margin, y + 1.5, self.l_margin, y + 9)
        self.set_x(self.l_margin + 5)
        self.set_font("Helvetica", "B", 7.5)
        self.set_text_color(*ACENTO)
        self.cell(0, 4, s(sobretitulo.upper()), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_x(self.l_margin + 5)
        self.set_font("Times", "B", 16)
        self.set_text_color(*TINTA)
        self.cell(0, 7, s(titulo), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(4)

    def p(self, texto: str, tamanho: float = 9.5) -> None:
        self.set_font("Helvetica", size=tamanho)
        self.set_text_color(*TINTA)
        self.multi_cell(0, 4.9, s(texto))
        self.ln(2.2)

    def nota(self, texto: str) -> None:
        self.set_font("Helvetica", "I", 8.5)
        self.set_text_color(*SUAVE)
        self.multi_cell(0, 4.3, s(texto))
        self.ln(2)

    def item(self, titulo: str, corpo: str) -> None:
        self.set_font("Helvetica", size=9)
        self.cabe_ou_quebra(6 + 4.4 * self._linhas(self.util, corpo) + 4)
        self.set_font("Helvetica", "B", 9.5)
        self.set_text_color(*TINTA)
        self.multi_cell(0, 4.8, s(titulo))
        self.set_font("Helvetica", size=9)
        self.set_text_color(*SUAVE)
        self.multi_cell(0, 4.4, s(corpo))
        self.ln(3)

    def cabe_ou_quebra(self, altura: float) -> None:
        """Quebra a pagina se o bloco nao cabe inteiro no que sobrou.

        Sem isso o auto page break parte a pergunta no meio e a ultima linha
        fica orfa no topo da pagina seguinte, longe do titulo dela — quem
        responde perde o contexto do que esta marcando."""
        if self.get_y() + altura > self.h - self.b_margin:
            self.add_page()

    def pergunta(
        self, n: int, titulo: str, corpo: str, opcoes: list[str], peso: str | None = None
    ) -> None:
        # Estimativa do bloco: titulo + corpo + uma linha por opcao + destaque.
        self.cabe_ou_quebra(24 + 5.5 * len(opcoes) + (12 if peso else 0))
        self.ln(1)
        self.set_font("Times", "B", 12)
        self.set_text_color(*TINTA)
        self.multi_cell(self.util, 5.8, s(f"{n}.  {titulo}"))
        self.ln(1.2)

        self.set_font("Helvetica", size=9)
        self.set_text_color(*TINTA)
        self.multi_cell(self.util, 4.6, s(corpo))
        self.ln(1.5)

        for op in opcoes:
            self.set_font("Helvetica", size=9)
            self.set_text_color(*TINTA)
            yq = self.get_y()
            self.set_draw_color(120, 120, 128)
            self.set_line_width(0.3)
            self.rect(self.l_margin + 1, yq + 0.7, 3.4, 3.4)
            self.set_x(self.l_margin + 8)
            self.multi_cell(self.util - 8, 4.7, s(op))
            self.ln(0.8)

        if peso:
            self.ln(1)
            y0 = self.get_y()
            self.set_font("Helvetica", "B", 8.5)
            self.set_text_color(*DESTAQUE)
            self.set_draw_color(*DESTAQUE)
            self.set_line_width(0.8)
            self.set_x(self.l_margin + 4)
            self.multi_cell(self.util - 4, 4.4, s(peso))
            self.line(self.l_margin + 1, y0 + 0.5, self.l_margin + 1, self.get_y() - 1)

        self.ln(3)
        self.set_draw_color(*REGUA)
        self.set_line_width(0.2)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(5)

    def tabela(
        self,
        cabecalho: list[str],
        linhas: list[list[str]],
        larguras: list[float],
        total: list[str] | None = None,
        col_destaque: int | None = None,
    ) -> None:
        """Prefixo '*' na primeira celula pinta a linha inteira de DESTAQUE.
        `col_destaque` pinta so aquela coluna, em todas as linhas."""
        self.set_font("Helvetica", "B", 6.5)
        self.set_text_color(*SUAVE)
        for i, (t, w) in enumerate(zip(cabecalho, larguras)):
            self.cell(w, 5, s(t.upper()), align="L" if i == 0 else "R")
        self.ln(5)
        self.set_draw_color(*REGUA)
        self.set_line_width(0.3)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(1.5)

        for linha in linhas:
            realce = linha[0].startswith("*")
            for i, (t, w) in enumerate(zip(linha, larguras)):
                pinta = realce or i == col_destaque
                self.set_font("Helvetica", "B" if pinta else "", 8)
                self.set_text_color(*(DESTAQUE if pinta else TINTA))
                self.cell(w, 5.4, s(t.lstrip("*")), align="L" if i == 0 else "R")
            self.ln(5.4)

        if total:
            self.ln(0.5)
            self.set_draw_color(*REGUA)
            self.set_line_width(0.3)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.ln(1.5)
            for i, (t, w) in enumerate(zip(total, larguras)):
                self.set_font("Helvetica", "B", 8)
                self.set_text_color(*(DESTAQUE if i == col_destaque else TINTA))
                self.cell(w, 5.4, s(t), align="L" if i == 0 else "R")
            self.ln(5.4)
        self.ln(3)

    def _linhas(self, largura: float, texto: str) -> int:
        """Quantas linhas o texto ocupa, sem desenhar. Precisa medir ANTES para
        o fundo ser desenhado primeiro — retangulo depois do texto pinta por
        cima dele."""
        return len(self.multi_cell(largura, 5, s(texto), dry_run=True, output="LINES"))

    def selo(self, titulo: str, corpo: str) -> None:
        """Regra ja decidida. Nao e pergunta: e o que vai ser aplicado."""
        largura = self.util - 12
        self.set_font("Helvetica", "B", 10)
        n_tit = self._linhas(largura, titulo)
        self.set_font("Helvetica", size=8.8)
        n_cor = self._linhas(largura, corpo)
        alt = 5.2 * n_tit + 4.5 * n_cor + 9

        self.cabe_ou_quebra(alt + 4)
        self.ln(1)
        y0 = self.get_y()
        self.set_fill_color(*FUNDO_ACENTO)
        self.rect(self.l_margin, y0, self.util, alt, style="F")

        self.set_y(y0 + 4)
        self.set_x(self.l_margin + 6)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*ACENTO)
        self.multi_cell(largura, 5.2, s(titulo))
        self.set_x(self.l_margin + 6)
        self.set_font("Helvetica", size=8.8)
        self.set_text_color(*TINTA)
        self.multi_cell(largura, 4.5, s(corpo))
        self.set_y(y0 + alt)
        self.ln(4)

    def conta(self, linhas: list[tuple[str, str, str]]) -> None:
        """Bloco de comparacao de calculo: expressao, resultado, glosa."""
        self.ln(1)
        y0 = self.get_y()
        alt = 6.6 * len(linhas) + 6
        self.set_fill_color(*FUNDO_BOX)
        self.rect(self.l_margin, y0, self.util, alt, style="F")
        self.set_y(y0 + 3.5)
        for expr, res, glosa in linhas:
            self.set_x(self.l_margin + 6)
            self.set_font("Courier", "", 9.5)
            self.set_text_color(*TINTA)
            self.cell(46, 5, s(expr))
            self.set_font("Courier", "B", 9.5)
            self.set_text_color(*ACENTO)
            self.cell(26, 5, s(res))
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(*SUAVE)
            self.cell(0, 5, s(glosa), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.ln(1.6)
        self.set_y(y0 + alt)
        self.ln(4)


def build() -> Path:
    pdf = Doc(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_margins(20, 18, 20)
    pdf.add_page()

    # ── abertura ──────────────────────────────────────────────────────────
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 7.5)
    pdf.set_text_color(*ACENTO)
    pdf.cell(
        0,
        4,
        s("PARA VALIDAÇÃO  ·  RAFAEL  ·  25 DE AGOSTO DE 2026"),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.ln(3)
    pdf.set_font("Times", "B", 30)
    pdf.set_text_color(*TINTA)
    pdf.multi_cell(0, 12.5, s("Da Nasmar para a MM"))
    pdf.ln(1.5)
    pdf.set_font("Helvetica", size=12.5)
    pdf.set_text_color(*SUAVE)
    pdf.multi_cell(
        0,
        6,
        s(
            "As regras que o sistema vai passar a aplicar sozinho — "
            "e as cinco decisões que dependem de você."
        ),
    )
    pdf.ln(4)
    pdf.set_draw_color(*ACENTO)
    pdf.set_line_width(0.7)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + 26, pdf.get_y())
    pdf.ln(7)

    pdf.p(
        "Hoje a Grazi cadastra o mesmo pedido duas vezes: uma no sistema da Nasmar, "
        "com o cliente final, e outra no da MM, trocando o cliente para Nasmar e "
        "ajustando o preço. O sistema não participa de nenhuma das duas."
    )
    pdf.p(
        "A proposta é o sistema fazer as duas pernas sozinho. Antes de ligar isso, "
        "cinco regras precisam da sua confirmação — todas mexem em valor de "
        "nota fiscal, então nenhuma vai por suposição."
    )
    pdf.nota(
        "Todos os números deste documento foram medidos nos dois bancos reais, "
        "Nasmar e MM Americanense, em 24 e 25 de agosto de 2026. Nada aqui é "
        "estimativa."
    )

    # ── como vai funcionar ────────────────────────────────────────────────
    pdf.secao("Como vai funcionar", "As duas pernas, automáticas")

    y = pdf.get_y()
    pdf.set_fill_color(*FUNDO_ACENTO)
    pdf.rect(pdf.l_margin, y, pdf.util, 26, style="F")
    pdf.set_xy(pdf.l_margin + 7, y + 5)
    pdf.set_font("Helvetica", "B", 10.5)
    pdf.set_text_color(*ACENTO)
    pdf.cell(
        0, 5.5, s("DAJU   >   NASMAR   >   MM AMERICANENSE"), new_x=XPos.LMARGIN, new_y=YPos.NEXT
    )
    pdf.set_x(pdf.l_margin + 7)
    pdf.set_font("Helvetica", size=8.5)
    pdf.set_text_color(*TINTA)
    pdf.multi_cell(
        pdf.util - 14,
        4.4,
        s(
            "O cliente compra da Nasmar. A Nasmar compra da MM. A MM produz e "
            "fatura para a Nasmar, que fatura para o cliente."
        ),
    )
    pdf.set_y(y + 26)
    pdf.ln(5)

    pdf.p("Quando chega um pedido de um cliente que compra da Nasmar, o sistema:")
    for passo in [
        "1.   Cadastra o pedido no sistema da NASMAR, com o cliente final e o preço da nota.",
        "2.   Guarda esse pedido numa lista do período — semana ou quinzena.",
        "3.   No fim do período, junta todos os pedidos da lista num pedido só "
        "para a MM, com a Nasmar como cliente e o preço ajustado pelo percentual.",
        "4.   Anota nesse pedido da MM quais pedidos da Nasmar entraram nele.",
    ]:
        pdf.set_font("Helvetica", size=9.5)
        pdf.set_text_color(*TINTA)
        pdf.multi_cell(0, 4.9, s(passo))
        pdf.ln(1.6)
    pdf.ln(1)
    pdf.nota(
        "O pedido da MM não vai direto para o sistema: ele aparece na tela para "
        "conferência antes de ser gravado. Um pedido de semana cheia tem cerca de "
        "170 linhas — se nascer errado, erra tudo de uma vez."
    )

    # ── o desvio de hoje ──────────────────────────────────────────────────
    pdf.add_page()
    pdf.secao("O que os números mostram", "O desvio de hoje")

    pdf.p(
        "Comparamos todos os pedidos de 2026 que aparecem nos dois sistemas: "
        "75 pedidos, R$ 3,27 milhões em nota. Para cada um, olhamos o que foi "
        "faturado da MM para a Nasmar e o que a regra dos 7% mandaria faturar."
    )
    pdf.p("O resultado é que quase nada segue a regra.")

    pdf.tabela(
        ["Cliente final", "Ped.", "Nota Nasmar", "Faturado MM", "Devido (/1,07)", "Desvio"],
        [
            ["Beira Rio", "5", "1.813.866,00", "1.728.764,69", "1.695.201,87", "+33.562,82"],
            [
                "*Calcenter (Centauro)",
                "60",
                "1.084.111,80",
                "1.079.212,92",
                "1.013.188,60",
                "+66.024,32",
            ],
            ["Dakota Nordeste", "3", "268.080,00", "247.680,00", "250.542,06", "-2.862,06"],
            ["DAJU", "1", "76.932,00", "71.899,08", "71.899,07", "+0,01"],
            ["Outros seis clientes", "6", "23.774,30", "23.774,30", "22.218,97", "+1.555,33"],
        ],
        [50, 12, 28, 28, 28, 24],
        total=["Total", "75", "3.266.764,10", "3.151.330,99", "3.053.050,56", "+98.280,43"],
        col_destaque=5,
    )

    pdf.nota("Valores em reais. Desvio positivo = faturado ACIMA do que a regra manda.")
    pdf.ln(1)

    pdf.item(
        "Faturamos R$ 101.142,49 acima da regra em 2026",
        "Base de cálculo inflada nesse valor, com o imposto correspondente pago a "
        "mais. Não é dinheiro que volta sozinho.",
    )
    pdf.item(
        "E R$ 2.862,06 abaixo, na Dakota",
        "O erro acontece nas duas direções. Nesse caso a base ficou a menor.",
    )
    pdf.item(
        "Um único pedido em 75 bateu a regra",
        "O da DAJU, de 24 de agosto, com R$ 0,01 de diferença. Todos os outros 74 "
        "desviaram para mais ou para menos.",
    )
    pdf.item(
        "O Centauro nunca teve o ajuste aplicado",
        "Sessenta pedidos, R$ 1,08 milhão, todos faturados pelo valor cheio da nota. "
        "Sozinho responde por R$ 66.024,32 dos R$ 101 mil de base inflada. Não é "
        "regra diferente para ele — é o desconto que nunca foi feito.",
    )

    pdf.p(
        "Nada disso é falha de quem digita: são centenas de pedidos por ano, cada um "
        "com dezenas de linhas, e a conta é feita à mão. É exatamente o tipo de erro "
        "que some quando o cálculo passa a ser do sistema.",
    )

    # ── regras definidas ──────────────────────────────────────────────────
    pdf.secao("Definido", "Duas regras que já estão fechadas")

    pdf.selo(
        "O período é semanal",
        "Continua parametrizável — dá para virar quinzenal na tela quando quiser, "
        "sem depender de programador. Mas entra ligado em semanal.",
    )
    pdf.selo(
        "O percentual e o método valem para TODOS os clientes",
        "Sem exceção cadastrada, inclusive Centauro. É o que corrige o desvio "
        "mostrado acima: a mesma conta para todo mundo, feita pelo sistema, "
        "em todo pedido.",
    )

    # ── decisoes ──────────────────────────────────────────────────────────
    pdf.add_page()
    pdf.secao("Decisões", "Três confirmações")

    pdf.set_font("Times", "B", 12)
    pdf.set_text_color(*TINTA)
    pdf.multi_cell(pdf.util, 5.8, s("1.  A conta é dividir por 1,07 ou tirar 7% do valor?"))
    pdf.ln(1.2)
    pdf.set_font("Helvetica", size=9)
    pdf.multi_cell(
        pdf.util,
        4.6,
        s(
            "A frase pode ser lida de dois jeitos, e eles dão valores diferentes. "
            "Um produto que sai a R$ 16,12 na nota da Nasmar:"
        ),
    )
    pdf.conta(
        [
            ("16,12 / 1,07", "15,07", "é a conta usada no quadro acima"),
            ("16,12 x 0,93", "14,99", 'leitura literal de "menos 7%"'),
        ]
    )
    for op in [
        "Dividir por 1,07 - mantém a conta que a operação tenta fazer hoje",
        "Tirar 7% do valor - muda a base em relação ao que vinha sendo feito",
    ]:
        yq = pdf.get_y()
        pdf.set_draw_color(120, 120, 128)
        pdf.set_line_width(0.3)
        pdf.rect(pdf.l_margin + 1, yq + 0.7, 3.4, 3.4)
        pdf.set_x(pdf.l_margin + 8)
        pdf.set_font("Helvetica", size=9)
        pdf.set_text_color(*TINTA)
        pdf.multi_cell(pdf.util - 8, 4.7, s(op))
        pdf.ln(0.8)
    pdf.ln(1)
    y0 = pdf.get_y()
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(*DESTAQUE)
    pdf.set_x(pdf.l_margin + 4)
    pdf.multi_cell(
        pdf.util - 4,
        4.4,
        s(
            "Diferença de 0,46% do valor. Sobre os R$ 3,27 milhões de 2026, são "
            "cerca de R$ 15 mil por ano de base de cálculo."
        ),
    )
    pdf.set_draw_color(*DESTAQUE)
    pdf.set_line_width(0.8)
    pdf.line(pdf.l_margin + 1, y0 + 0.5, pdf.l_margin + 1, pdf.get_y() - 1)
    pdf.ln(3)
    pdf.set_draw_color(*REGUA)
    pdf.set_line_width(0.2)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(5)

    pdf.pergunta(
        2,
        "Que dia a semana fecha?",
        "O fechamento é o momento em que o pedido da MM é montado. Pedido que "
        "chegar depois entra na semana seguinte - a fechada não reabre.",
        [
            "Segunda de manhã, fechando a semana anterior",
            "Sexta à noite, fechando a própria semana",
            "Outro dia:  ______________________________",
        ],
    )

    pdf.pergunta(
        3,
        "Quando o percentual mudar, vale a partir de quando?",
        "A proposta é congelar o percentual no momento em que a semana fecha. "
        "Mudar o parâmetro depois não mexe em pedido já montado.",
        [
            "Sim - só vale para as semanas seguintes",
            "Não - quero poder recalcular semana já fechada",
        ],
    )
    # ── ja definido ───────────────────────────────────────────────────────
    pdf.secao("Já definido", "O que não precisa de resposta")
    pdf.p(
        "Estas regras já vieram da conversa e da forma como a operação "
        "trabalha hoje. Estão aqui só para você conferir se algo destoa."
    )

    for titulo, corpo in [
        (
            "Um pedido por semana, com todos os clientes juntos",
            "Não é um pedido por cliente nem por marca. Tudo que entrou na Nasmar "
            "na semana vira um pedido só para a MM.",
        ),
        (
            "Produtos iguais somam quantidade",
            "Se três clientes pediram a mesma meia para a mesma data, vira uma linha "
            "só com a soma. É o que já é feito hoje.",
        ),
        (
            "Menos quando o preço é diferente",
            "Achamos um caso real: rede Nacional Lojas, mesma meia a R$ 32,00 num centro "
            "de distribuição e R$ 36,00 em seis outros, na mesma semana. Aí "
            "viram duas linhas, cada uma com seu preço. Juntar obrigaria a escolher um "
            "valor — e escolher errado é nota errada.",
        ),
        (
            "Sem percentual definido, a semana não fecha",
            "O sistema para e pergunta, em vez de assumir um valor. Preço errado em "
            "nota é imposto errado.",
        ),
        (
            "A data de entrega passa a ser a mais próxima entre os pedidos",
            "Hoje ela é digitada à mão e já saiu errada: existe pedido de "
            "agosto de 2026 gravado com entrega em 2028.",
        ),
    ]:
        pdf.item(titulo, corpo)

    pdf.secao("Antes de ligar", "Como entramos em produção")
    pdf.p(
        "O sistema nunca gravou pedido nesses bancos — hoje quem grava é sempre "
        "uma pessoa. Por isso o primeiro pedido gerado será conferido campo a campo "
        "contra um pedido feito à mão, numa cópia do banco, antes de "
        "qualquer coisa tocar o sistema de verdade. E a primeira semana fechada "
        "vai ser conferido por vocês antes de faturar."
    )

    pdf.ln(3)
    y = pdf.get_y()
    pdf.set_fill_color(*FUNDO_BOX)
    pdf.rect(pdf.l_margin, y, pdf.util, 20, style="F")
    pdf.set_xy(pdf.l_margin + 7, y + 5)
    pdf.set_font("Helvetica", "B", 9.5)
    pdf.set_text_color(*TINTA)
    pdf.cell(
        0,
        5,
        s("Respondidas as três perguntas, o resto já está pronto para começar."),
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.set_x(pdf.l_margin + 7)
    pdf.set_font("Helvetica", size=8.5)
    pdf.set_text_color(*SUAVE)
    pdf.cell(0, 4.5, s("Samuel  ·  25 de agosto de 2026"))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(OUTPUT))
    return OUTPUT


if __name__ == "__main__":
    caminho = build()
    print(f"PDF gerado: {caminho}  ({caminho.stat().st_size:,} bytes)")
