"""
Gera o PDF de ajuda do Portal de Pedidos.
Uso: .venv/bin/python tools/generate_help_pdf.py
"""

from datetime import date
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

OUTPUT = Path(__file__).parent.parent / "docs" / "help" / "guia-portal-pedidos.pdf"

# Substitui caracteres fora do latin-1 por equivalentes ASCII seguros
_REPLACEMENTS = [
    ("\u2014", " - "),
    ("\u2013", "-"),
    ("\u2192", " > "),
    ("\u2190", " < "),
    ("\u2191", "^"),
    ("\u2193", "v"),
    ("\u2022", "*"),
    ("\u25e6", "o"),
    ("\u00b7", "*"),
    ("\u2019", "'"),
    ("\u2018", "'"),
    ("\u201d", "?"),
    ("\u201c", "?"),
    ("\u2260", "!="),
    ("\u2264", "<="),
    ("\u2265", ">="),
    ("\u00b1", "+/-"),
    ("\u00d7", "x"),
    ("\u00f7", "/"),
    ("\u00b0", "o"),
    ("\u2500", "-"),
    ("\u2502", "|"),
]


def _s(text: str) -> str:
    for src, dst in _REPLACEMENTS:
        text = text.replace(src, dst)
    return text

# ── Paleta ───────────────────────────────────────────────────────────────────
DARK_BG   = (15,  17,  21)
SURFACE   = (24,  28,  36)
ACCENT    = (79, 143, 255)
TEXT_PRI  = (230, 232, 240)
TEXT_SEC  = (140, 148, 168)
DIVIDER   = (40,  46,  58)
SUCCESS   = (52, 199, 89)
WARNING   = (255, 159, 10)
TABLE_H   = (30,  35,  47)
TABLE_R1  = (20,  24,  32)
TABLE_R2  = (24,  29,  40)


class Doc(FPDF):
    _page_label_skip = True  # skip footer on cover

    def normalize_text(self, text: str) -> str:  # noqa: D401
        return super().normalize_text(_s(text))

    def header(self):
        if self.page_no() == 1:
            return
        # thin top bar
        self.set_fill_color(*SURFACE)
        self.rect(0, 0, 210, 12, "F")
        self.set_text_color(*TEXT_SEC)
        self.set_font("Helvetica", size=7)
        self.set_xy(14, 4)
        self.cell(0, 4, "Portal de Pedidos — Guia de Uso", align="L")
        self.set_xy(0, 4)
        self.cell(196, 4, _s(f"SamFlowsAI * {date.today().strftime('%d/%m/%Y')}"), align="R")

    def footer(self):
        if self.page_no() == 1:
            return
        self.set_y(-12)
        self.set_fill_color(*SURFACE)
        self.rect(0, 285, 210, 12, "F")
        self.set_text_color(*TEXT_SEC)
        self.set_font("Helvetica", size=7)
        self.cell(0, 6, f"Página {self.page_no() - 1}", align="C")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def bg(self):
        self.set_fill_color(*DARK_BG)
        self.rect(0, 0, 210, 297, "F")

    def section_title(self, number: str, title: str):
        self.ln(6)
        # accent pill
        self.set_fill_color(*ACCENT)
        self.set_text_color(*DARK_BG)
        self.set_font("Helvetica", "B", 8)
        self.set_x(14)
        pill_w = self.get_string_width(number) + 6
        self.cell(pill_w, 6, number, fill=True, border=0)
        self.set_text_color(*TEXT_PRI)
        self.set_font("Helvetica", "B", 13)
        self.set_x(14 + pill_w + 3)
        self.cell(0, 6, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(2)
        # divider
        self.set_draw_color(*ACCENT)
        self.set_line_width(0.4)
        self.line(14, self.get_y(), 196, self.get_y())
        self.ln(4)

    def sub_title(self, label: str):
        self.set_text_color(*ACCENT)
        self.set_font("Helvetica", "B", 10)
        self.set_x(14)
        self.cell(0, 6, label, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

    def body(self, text: str, indent: int = 14):
        self.set_text_color(*TEXT_PRI)
        self.set_font("Helvetica", size=9)
        self.set_x(indent)
        self.multi_cell(196 - indent + 14, 5, text)
        self.ln(1)

    def bullet(self, text: str, level: int = 1):
        indent = 18 + (level - 1) * 6
        bullet_char = "•" if level == 1 else "◦"
        self.set_text_color(*TEXT_PRI)
        self.set_font("Helvetica", size=9)
        self.set_x(indent)
        self.cell(5, 5, bullet_char)
        self.multi_cell(196 - indent + 14 - 5, 5, text)

    def step(self, number: int, text: str):
        self.set_x(18)
        self.set_fill_color(*ACCENT)
        self.set_text_color(*DARK_BG)
        self.set_font("Helvetica", "B", 7)
        self.cell(5, 5, str(number), fill=True, align="C")
        self.set_text_color(*TEXT_PRI)
        self.set_font("Helvetica", size=9)
        self.cell(172, 5, f"  {text}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

    def note_box(self, icon: str, text: str, color=None):
        if color is None:
            color = WARNING
        self.ln(2)
        self.set_fill_color(*SURFACE)
        self.set_draw_color(*color)
        self.set_line_width(0.5)
        x = 14
        y = self.get_y()
        # measure height
        self.set_font("Helvetica", size=8.5)
        lines_needed = max(1, len(self.multi_cell(162, 4.5, f"{icon}  {text}", dry_run=True, output="LINES")))
        h = lines_needed * 4.5 + 5
        self.rect(x, y, 182, h, style="FD")
        self.set_xy(x + 3, y + 2.5)
        self.set_text_color(*color)
        self.set_font("Helvetica", "B", 8.5)
        self.cell(6, 4.5, icon)
        self.set_text_color(*TEXT_PRI)
        self.set_font("Helvetica", size=8.5)
        self.set_xy(x + 9, y + 2.5)
        self.multi_cell(170, 4.5, text)
        self.set_y(y + h + 2)

    def table(self, headers: list[str], rows: list[list[str]], col_widths: list[float] | None = None):
        total_w = 182.0
        n = len(headers)
        if col_widths is None:
            col_widths = [total_w / n] * n

        # header row
        self.set_fill_color(*TABLE_H)
        self.set_text_color(*ACCENT)
        self.set_font("Helvetica", "B", 8)
        self.set_x(14)
        for i, h in enumerate(headers):
            self.cell(col_widths[i], 7, h, border=0, fill=True, align="L")
        self.ln()
        self.set_draw_color(*DIVIDER)
        self.set_line_width(0.2)
        self.line(14, self.get_y(), 196, self.get_y())

        for ri, row in enumerate(rows):
            fill_color = TABLE_R1 if ri % 2 == 0 else TABLE_R2
            self.set_fill_color(*fill_color)
            self.set_text_color(*TEXT_PRI)
            self.set_font("Helvetica", size=8)
            self.set_x(14)
            # measure max height for this row
            max_lines = 1
            for ci, cell in enumerate(row):
                lines = self.multi_cell(col_widths[ci] - 2, 5, cell, dry_run=True, output="LINES")
                max_lines = max(max_lines, len(lines))
            row_h = max_lines * 5 + 2

            # check page break
            if self.get_y() + row_h > 277:
                self.add_page()
                self.bg()
                self.set_fill_color(*TABLE_H)
                self.set_text_color(*ACCENT)
                self.set_font("Helvetica", "B", 8)
                self.set_x(14)
                for i, h in enumerate(headers):
                    self.cell(col_widths[i], 7, h, border=0, fill=True, align="L")
                self.ln()
                self.line(14, self.get_y(), 196, self.get_y())

            y_row = self.get_y()
            self.set_x(14)
            for ci, cell_text in enumerate(row):
                self.set_fill_color(*fill_color)
                self.rect(14 + sum(col_widths[:ci]), y_row, col_widths[ci], row_h, "F")
                self.set_xy(14 + sum(col_widths[:ci]) + 1, y_row + 1)
                self.multi_cell(col_widths[ci] - 2, 5, cell_text)
            self.set_y(y_row + row_h)
        self.ln(4)

    def flow_diagram(self, steps: list[tuple[str, str]]):
        """Horizontal flow diagram with arrow connectors."""
        n = len(steps)
        box_w = min(30.0, 160.0 / n)
        total = box_w * n + 6 * (n - 1)
        x_start = (210 - total) / 2
        y = self.get_y() + 2
        box_h = 14

        for i, (label, sub) in enumerate(steps):
            x = x_start + i * (box_w + 6)
            # box
            self.set_fill_color(*SURFACE)
            self.set_draw_color(*ACCENT)
            self.set_line_width(0.4)
            self.rect(x, y, box_w, box_h, "FD")
            # label
            self.set_text_color(*ACCENT)
            self.set_font("Helvetica", "B", 6.5)
            self.set_xy(x, y + 2)
            self.cell(box_w, 5, label, align="C")
            # sub
            self.set_text_color(*TEXT_SEC)
            self.set_font("Helvetica", size=5.5)
            self.set_xy(x, y + 7)
            self.cell(box_w, 4, sub, align="C")
            # arrow
            if i < n - 1:
                ax = x + box_w + 0.5
                ay = y + box_h / 2
                self.set_draw_color(*TEXT_SEC)
                self.set_line_width(0.3)
                self.line(ax, ay, ax + 5, ay)
                # arrowhead
                self.line(ax + 5, ay, ax + 3.5, ay - 1.5)
                self.line(ax + 5, ay, ax + 3.5, ay + 1.5)

        self.set_y(y + box_h + 6)


# ─────────────────────────────────────────────────────────────────────────────
# BUILD
# ─────────────────────────────────────────────────────────────────────────────

def build():
    pdf = Doc(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(True, margin=16)
    pdf.set_margins(14, 16, 14)

    # ── CAPA ─────────────────────────────────────────────────────────────────
    pdf.add_page()
    pdf.bg()

    # gradient-like bands
    for i, alpha in enumerate(range(15, 50, 5)):
        pdf.set_fill_color(15 + i, 17 + i, 21 + i * 2)
        pdf.rect(0, 297 - (i + 1) * 12, 210, 12, "F")

    # accent bar top
    pdf.set_fill_color(*ACCENT)
    pdf.rect(0, 0, 210, 3, "F")

    # Logo / brand
    pdf.set_xy(0, 55)
    pdf.set_text_color(*TEXT_SEC)
    pdf.set_font("Helvetica", size=10)
    pdf.cell(210, 8, "SamFlowsAI", align="C")

    # Title
    pdf.set_y(68)
    pdf.set_text_color(*TEXT_PRI)
    pdf.set_font("Helvetica", "B", 28)
    pdf.cell(210, 12, "Portal de Pedidos", align="C")

    pdf.set_y(83)
    pdf.set_text_color(*ACCENT)
    pdf.set_font("Helvetica", size=14)
    pdf.cell(210, 8, "Guia de Uso", align="C")

    # Divider
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.6)
    pdf.line(60, 96, 150, 96)

    # Subtitle description
    pdf.set_y(102)
    pdf.set_text_color(*TEXT_SEC)
    pdf.set_font("Helvetica", size=10)
    pdf.cell(210, 6, "Importação de pedidos via PDF e XLS  ·  Preview e validação", align="C")
    pdf.set_y(110)
    pdf.cell(210, 6, "Exportação XLSX  ·  Integração direta Fire ERP", align="C")

    # Flow mini preview on cover
    pdf.set_y(128)
    pdf.flow_diagram([
        ("Arquivo", "PDF / XLS"),
        ("Preview", "Validação"),
        ("Confirmar", "Aprovar"),
        ("XLSX", "Excel local"),
        ("Fire ERP", "Direto no banco"),
    ])

    # Index block
    idx_y = 170
    pdf.set_fill_color(*SURFACE)
    pdf.rect(30, idx_y, 150, 78, "F")
    pdf.set_draw_color(*DIVIDER)
    pdf.set_line_width(0.2)
    pdf.rect(30, idx_y, 150, 78, "D")
    pdf.set_xy(30, idx_y + 4)
    pdf.set_text_color(*ACCENT)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(150, 6, "Conteúdo", align="C")
    items = [
        ("01", "Visão Geral do Sistema"),
        ("02", "Importando um Pedido"),
        ("03", "Ajustes Antes de Exportar"),
        ("04", "Enviando para o Fire ERP"),
        ("05", "Configurações"),
        ("06", "Dúvidas Frequentes"),
    ]
    pdf.set_y(idx_y + 12)
    for num, label in items:
        pdf.set_x(38)
        pdf.set_text_color(*ACCENT)
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(10, 6, num)
        pdf.set_text_color(*TEXT_PRI)
        pdf.set_font("Helvetica", size=8)
        pdf.cell(120, 6, label, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # Date
    pdf.set_y(258)
    pdf.set_text_color(*TEXT_SEC)
    pdf.set_font("Helvetica", size=8)
    pdf.cell(210, 5, f"Versão 1.0  ·  {date.today().strftime('%d de %B de %Y')}", align="C")

    # bottom bar
    pdf.set_fill_color(*ACCENT)
    pdf.rect(0, 294, 210, 3, "F")

    # ── SEÇÃO 1 — VISÃO GERAL ────────────────────────────────────────────────
    pdf.add_page()
    pdf.bg()
    pdf.set_y(16)
    pdf.section_title("01", "Visão Geral do Sistema")

    pdf.body(
        "O Portal de Pedidos é uma plataforma web que automatiza a entrada de pedidos de compra "
        "recebidos de varejistas em PDF ou XLS/XLSX. O sistema identifica o formato do arquivo, "
        "extrai os dados automaticamente, apresenta um preview para validação humana e exporta "
        "diretamente para planilha Excel (XLSX) ou para o banco de dados do Fire ERP (Firebird)."
    )

    pdf.sub_title("Fluxo completo")
    pdf.flow_diagram([
        ("Upload", "PDF / XLS"),
        ("Extração", "Texto + tabelas"),
        ("Parser", "9 parsers + IA"),
        ("Preview", "Revisão humana"),
        ("Confirmar", "Gravar"),
        ("Exportar", "XLSX / Fire"),
    ])

    pdf.sub_title("Pontos de entrada")
    pdf.bullet("Upload direto: arraste o arquivo para a tela principal")
    pdf.bullet("Watch folder: pasta monitorada — arquivos aparecem na aba Pendentes automaticamente")
    pdf.ln(2)

    pdf.sub_title("Formatos suportados")
    pdf.table(
        ["Fornecedor / Layout", "Formato", "Detecção"],
        [
            ["Mercado Eletrônico", "PDF", "Automática"],
            ["Pedido Compras Revenda", "PDF", "Automática"],
            ["SBF / Centauro", "PDF", "Automática"],
            ["Beira Rio", "PDF", "Automática"],
            ["Kolosh", "PDF", "Automática"],
            ["Sam's Club (WebEDI / Neogrid)", "PDF", "Automática"],
            ["Kallan", "XLS", "Automática"],
            ["Desmembramento", "XLS", "Automática"],
            ["Genérico (heurística)", "PDF / XLS", "Fallback regex"],
            ["Qualquer layout desconhecido", "PDF / XLS", "Fallback IA (automático)"],
        ],
        col_widths=[80, 30, 72],
    )

    pdf.note_box("!", "Limite de upload: 50 MB por arquivo. Extensões aceitas: .pdf  .xls  .xlsx", WARNING)

    # ── SEÇÃO 2 — IMPORTANDO ─────────────────────────────────────────────────
    pdf.add_page()
    pdf.bg()
    pdf.set_y(16)
    pdf.section_title("02", "Importando um Pedido")

    pdf.sub_title("2.1  Via upload direto")
    for n, t in [
        (1, 'Acesse o portal no navegador: http://localhost:8000'),
        (2, "Faça login com seu e-mail e senha"),
        (3, 'Na tela principal, clique em "Carregar pedido" ou arraste o arquivo para a área de upload'),
        (4, "Aguarde o processamento (geralmente menos de 5 segundos)"),
        (5, "Revise o Preview exibido pelo sistema"),
        (6, 'Clique em "Confirmar" para gravar o pedido'),
    ]:
        pdf.step(n, t)

    pdf.ln(2)
    pdf.sub_title("O Preview mostra:")
    pdf.bullet("Cabeçalho: número do pedido, data de emissão, nome do cliente e CNPJ")
    pdf.bullet("Itens: descrição, código do produto, EAN, quantidade, preço unitário e total")
    pdf.bullet("Grupos de entrega: quando o pedido tem múltiplas lojas ou filiais, os itens aparecem agrupados")
    pdf.bullet("Verificação de produtos: o sistema compara os códigos com o catálogo do Fire e sinaliza divergências")
    pdf.ln(3)

    pdf.sub_title("2.2  Via pasta monitorada (watch folder)")
    for n, t in [
        (1, "Configure a pasta em Configurações → Diretórios"),
        (2, "Copie os arquivos PDF/XLS para essa pasta"),
        (3, 'Na tela principal, acesse a aba "Pendentes"'),
        (4, 'Clique em "Preview" ao lado do arquivo desejado'),
        (5, "Revise os dados e confirme"),
    ]:
        pdf.step(n, t)

    pdf.ln(2)
    pdf.note_box(
        "i",
        "A aba Pendentes é atualizada automaticamente quando novos arquivos são detectados na pasta monitorada.",
        ACCENT,
    )

    pdf.ln(3)
    pdf.sub_title("2.3  Dados extraídos automaticamente")
    pdf.table(
        ["Campo", "Descrição", "Exemplo"],
        [
            ["Número do pedido", "Identificador único do pedido", "PED-2024-00123"],
            ["Data de emissão", "Data em que o pedido foi emitido", "15/04/2024"],
            ["Cliente", "Razão social do comprador", "RIACHUELO S.A."],
            ["CNPJ do cliente", "CNPJ formatado ou somente dígitos", "33.200.056/0001-48"],
            ["Código do produto", "SKU interno do fornecedor", "CAL-001-38"],
            ["EAN", "Código de barras do produto", "7890123456789"],
            ["Quantidade", "Unidades do item", "120"],
            ["Preço unitário", "Valor por unidade", "R$ 89,90"],
            ["Data de entrega", "Prazo por item ou grupo", "30/05/2024"],
            ["Local de entrega", "CNPJ ou nome da loja destino", "SAM'S CLUB FILIAL 0094"],
        ],
        col_widths=[50, 80, 52],
    )

    # ── SEÇÃO 3 — AJUSTES ────────────────────────────────────────────────────
    pdf.add_page()
    pdf.bg()
    pdf.set_y(16)
    pdf.section_title("03", "Ajustes Antes de Exportar")

    pdf.body(
        "Antes de confirmar o pedido ou antes de enviá-lo ao Fire, você pode ajustar o modo de "
        "exportação, o diretório de saída e vincular manualmente o cliente quando o CNPJ não for "
        "encontrado no cadastro do Fire."
    )

    pdf.sub_title("3.1  Modo de exportação")
    pdf.body("Acesse Configurações → Diretórios para definir como o pedido será exportado:")
    pdf.table(
        ["Modo", "O que faz", "Quando usar"],
        [
            [
                "Apenas XLSX",
                "Gera arquivo(s) Excel na pasta de saída.\nNenhuma escrita no banco Fire.",
                "Quando a importação no ERP é feita manualmente",
            ],
            [
                "Apenas Fire",
                "Insere o pedido direto em CAB_VENDAS e CORPO_VENDAS no Firebird.\nNenhum arquivo gerado.",
                "Integração total com o Fire ERP configurada",
            ],
            [
                "Ambos",
                "Gera XLSX e insere no Fire simultaneamente.\nSe o Fire falhar, o XLSX permanece disponível.",
                "Transição ou operação com dupla garantia",
            ],
        ],
        col_widths=[32, 90, 60],
    )

    pdf.sub_title("3.2  Pasta de saída do XLSX")
    pdf.bullet("Acesse Configurações → Diretórios")
    pdf.bullet('Ajuste o campo "Pasta de saída" para o diretório desejado')
    pdf.bullet("O campo aceita caminhos absolutos; use o botão de navegação para evitar erros de digitação")
    pdf.ln(2)

    pdf.sub_title("3.3  Override de cliente (cliente não encontrado)")
    pdf.body(
        "Quando o sistema não localiza o cliente pelo CNPJ no cadastro do Fire, uma mensagem de "
        "alerta aparece durante a revisão:"
    )
    for n, t in [
        (1, 'No preview do pedido, localize o aviso "Cliente não encontrado no Fire"'),
        (2, "Use o campo de busca para pesquisar por razão social ou CNPJ"),
        (3, "Selecione o cliente correto na lista de resultados"),
        (4, "A vinculação é registrada no histórico de auditoria"),
    ]:
        pdf.step(n, t)

    pdf.ln(2)
    pdf.note_box(
        "!",
        "O override de cliente fica registrado e é utilizado automaticamente nas próximas "
        "importações do mesmo CNPJ — reduzindo a necessidade de intervenção manual ao longo do tempo.",
        SUCCESS,
    )

    pdf.ln(3)
    pdf.sub_title("3.4  Split automático por loja")
    pdf.body(
        "Pedidos com múltiplos pontos de entrega são divididos automaticamente em arquivos "
        "separados. A lógica de divisão segue a seguinte prioridade:"
    )
    pdf.table(
        ["Prioridade", "Condição", "Exemplo", "Resultado"],
        [
            ["1°", "EAN de loja presente", "Sam's Club GRADE", "1 arquivo por EAN de filial"],
            ["2°", "CNPJ de entrega ≠ CNPJ do cliente", "Riachuelo filiais", "1 arquivo por CNPJ"],
            ["3°", "Nome de loja presente (sem CNPJ)", "NBA lojas", "1 arquivo por nome de loja"],
            ["4°", "Sem dados de entrega específicos", "Pedido simples", "Arquivo único"],
        ],
        col_widths=[22, 55, 45, 60],
    )
    pdf.body("Nomenclatura gerada: CLIENTE_CNPJ_PEDIDO_SUFIXO.xlsx")
    pdf.body("Exemplos: CENTAURO_33200056_PED001_1.xlsx  |  SAMS_CLUB_PED045_SAMS_LOJA_0094_08.xlsx")

    # ── SEÇÃO 4 — FIRE ERP ───────────────────────────────────────────────────
    pdf.add_page()
    pdf.bg()
    pdf.set_y(16)
    pdf.section_title("04", "Enviando para o Fire ERP")

    pdf.sub_title("Pré-requisitos")
    pdf.bullet("Banco Firebird configurado em Configurações → Banco de Dados")
    pdf.bullet('Modo de exportação definido como "Apenas Fire" ou "Ambos"')
    pdf.bullet("Teste de conexão realizado com sucesso (botão Testar na tela de banco)")
    pdf.ln(2)

    pdf.sub_title("Passo a passo")
    for n, t in [
        (1, 'Após confirmar o pedido, ele aparece na aba "Importados" com status Parseado'),
        (2, 'Clique no botão "Enviar ao Fire" na linha do pedido'),
        (3, "O sistema insere o cabeçalho em CAB_VENDAS e os itens em CORPO_VENDAS"),
        (4, "O status muda para Enviado ao Fire com o código interno do Fire registrado"),
        (5, "Em caso de erro, o status fica como Erro — consulte a seção de Dúvidas Frequentes"),
    ]:
        pdf.step(n, t)

    pdf.ln(3)
    pdf.sub_title("Envio em lote")
    pdf.body(
        "Para enviar múltiplos pedidos de uma vez, selecione os pedidos na aba Importados "
        "e clique em Enviar selecionados ao Fire. O sistema processa cada pedido em sequência "
        "e exibe o resultado por item."
    )

    pdf.ln(2)
    pdf.sub_title("Dados gravados no Fire")
    pdf.table(
        ["Tabela Fire", "Campo", "Origem no pedido"],
        [
            ["CAB_VENDAS", "NUMERO_PEDIDO_CLIENTE", "Número do pedido extraído"],
            ["CAB_VENDAS", "DATA_PEDIDO", "Data de emissão"],
            ["CAB_VENDAS", "CODIGO_CLIENTE", "FK para CADASTRO (por CNPJ)"],
            ["CAB_VENDAS", "STATUS", "PEDIDO (padrão)"],
            ["CORPO_VENDAS", "CODIGO_PRODUTO", "Código do produto"],
            ["CORPO_VENDAS", "QUANTIDADE", "Quantidade do item"],
            ["CORPO_VENDAS", "PRECO_UNITARIO", "Preço unitário"],
            ["CORPO_VENDAS", "VALOR_TOTAL", "Total calculado"],
            ["CORPO_VENDAS", "DATA_ENTREGA", "Data de entrega do item"],
        ],
        col_widths=[42, 60, 80],
    )

    pdf.note_box(
        "i",
        "O sistema detecta pedidos duplicados automaticamente (por número + cliente). "
        "Ao tentar inserir um pedido já existente no Fire, o sistema bloqueia e exibe o código já registrado.",
        ACCENT,
    )

    # ── SEÇÃO 5 — CONFIGURAÇÕES ──────────────────────────────────────────────
    pdf.add_page()
    pdf.bg()
    pdf.set_y(16)
    pdf.section_title("05", "Configurações")

    pdf.sub_title("5.1  Banco de Dados  (Configurações → Banco de Dados)")
    pdf.table(
        ["Campo", "Descrição", "Padrão"],
        [
            ["Caminho do banco", "Arquivo .fdb local ou caminho remoto (servidor Windows/Linux)", "—"],
            ["Host", "IP ou hostname do servidor Firebird. Deixe vazio para banco local (embedded)", "vazio"],
            ["Porta", "Porta TCP do servidor Firebird", "3050"],
            ["Usuário", "Usuário do banco Firebird", "SYSDBA"],
            ["Charset", "Encoding do banco. Fire Sistemas usa WIN1252 por padrão", "WIN1252"],
            ["Senha", "Armazenada criptografada; nunca exibida na interface", "—"],
        ],
        col_widths=[42, 104, 36],
    )
    pdf.note_box(
        "i",
        'Use o botão "Testar conexão" antes de salvar — ele valida as credenciais sem alterar nada no banco.',
        ACCENT,
    )

    pdf.ln(2)
    pdf.sub_title("5.2  Diretórios  (Configurações → Diretórios)")
    pdf.table(
        ["Campo", "Descrição"],
        [
            ["Pasta de entrada (watch folder)", "Pasta monitorada onde os arquivos PDF/XLS devem ser colocados"],
            ["Pasta de saída", "Onde os arquivos XLSX gerados serão salvos"],
            ["Modo de exportação", "XLSX / Fire / Ambos — define o destino ao confirmar o pedido"],
        ],
        col_widths=[72, 110],
    )

    pdf.ln(2)
    pdf.sub_title("5.3  Usuários  (Configurações → Usuários)")
    pdf.table(
        ["Ação", "Quem pode", "Descrição"],
        [
            ["Criar usuário", "Admin", "Define e-mail, papel (Admin / Operador) e senha inicial"],
            ["Enviar convite", "Admin", "Gera link de convite válido por 7 dias; o usuário define a própria senha"],
            ["Resetar senha", "Admin", "Invalida todas as sessões ativas do usuário e exige nova senha"],
            ["Desativar / Reativar", "Admin", "Bloqueia ou restaura acesso sem excluir o histórico"],
            ["Visualizar pedidos", "Admin e Operador", "Acesso total ao histórico de importações"],
            ["Alterar configurações", "Admin", "Banco de dados, diretórios e modo de exportação"],
        ],
        col_widths=[44, 38, 100],
    )

    # ── SEÇÃO 6 — FAQ ────────────────────────────────────────────────────────
    pdf.add_page()
    pdf.bg()
    pdf.set_y(16)
    pdf.section_title("06", "Dúvidas Frequentes")

    faq = [
        (
            "O arquivo foi carregado mas os dados parecem incorretos",
            "O sistema utilizou o parser genérico ou o fallback por IA. Revise o preview com atenção "
            "antes de confirmar. Se o formato for recorrente, avise o administrador para que um parser "
            "dedicado seja criado.",
        ),
        (
            "O pedido gerou vários arquivos XLSX",
            "Comportamento esperado. O sistema divide automaticamente pedidos com múltiplas lojas ou "
            "filiais de entrega (ex: Riachuelo, Sam's Club). Cada arquivo corresponde a uma loja.",
        ),
        (
            "Cliente não encontrado no Fire",
            "Use o campo de busca de clientes para localizar manualmente por razão social ou CNPJ. "
            "Após selecionar, a vinculação fica salva no histórico e evita a mesma situação no futuro.",
        ),
        (
            "Erro ao enviar para o Fire — conexão recusada",
            "Verifique em Configurações → Banco de Dados se o host, porta e credenciais estão corretos. "
            "Use Testar conexão. Confirme que o servidor Firebird está ativo e acessível na rede.",
        ),
        (
            "Pedido duplicado — sistema bloqueou o envio",
            "O sistema detectou que já existe um pedido com o mesmo número para o mesmo cliente no Fire. "
            "Verifique o histórico na aba Importados para ver o registro anterior.",
        ),
        (
            "Como reimportar um pedido com erro?",
            "Na aba Importados, localize o pedido com status Erro e clique em Reimportar. "
            "O sistema reprocessa o arquivo original do início.",
        ),
        (
            "Esqueci a senha",
            "Solicite ao administrador do sistema o reset de senha. O admin acessa Configurações → "
            "Usuários, localiza sua conta e clica em Resetar senha.",
        ),
        (
            "Posso processar vários arquivos ao mesmo tempo?",
            "Sim. Coloque todos os arquivos na pasta monitorada (watch folder) e use o botão "
            "Processar todos na aba Pendentes. Cada arquivo é processado de forma independente.",
        ),
    ]

    for q, a in faq:
        pdf.set_fill_color(*SURFACE)
        x = 14
        y = pdf.get_y()
        pdf.set_font("Helvetica", "B", 9)
        q_lines = pdf.multi_cell(178, 5, q, dry_run=True, output="LINES")
        pdf.set_font("Helvetica", size=8.5)
        a_lines = pdf.multi_cell(172, 4.5, a, dry_run=True, output="LINES")
        total_h = len(q_lines) * 5 + len(a_lines) * 4.5 + 10

        if y + total_h > 272:
            pdf.add_page()
            pdf.bg()
            y = pdf.get_y()

        pdf.rect(x, y, 182, total_h, "F")
        pdf.set_draw_color(*DIVIDER)
        pdf.set_line_width(0.2)
        pdf.rect(x, y, 182, total_h, "D")
        # left accent bar
        pdf.set_fill_color(*ACCENT)
        pdf.rect(x, y, 2, total_h, "F")

        pdf.set_xy(x + 5, y + 3)
        pdf.set_text_color(*TEXT_PRI)
        pdf.set_font("Helvetica", "B", 9)
        pdf.multi_cell(174, 5, q)

        pdf.set_x(x + 5)
        pdf.set_text_color(*TEXT_SEC)
        pdf.set_font("Helvetica", size=8.5)
        pdf.multi_cell(172, 4.5, a)

        pdf.set_y(y + total_h + 4)

    # final accent bar
    pdf.set_y(270)
    pdf.set_text_color(*TEXT_SEC)
    pdf.set_font("Helvetica", size=8)
    pdf.cell(0, 5, "Portal de Pedidos  ·  SamFlowsAI  ·  Suporte: samucaalves@gmail.com", align="C")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(OUTPUT))
    print(f"PDF gerado: {OUTPUT}")


if __name__ == "__main__":
    build()
