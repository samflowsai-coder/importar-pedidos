"""
Gera o PDF do Guia de Instalacao no Servidor (client-facing).
Reaproveita o design system de generate_help_pdf.py (dark-first, SamFlowsAI).

Uso: .venv/bin/python tools/generate_install_pdf.py [saida.pdf]
Fonte de verdade: INSTALACAO-SERVIDOR.md
"""

import sys
from datetime import date
from pathlib import Path

from fpdf.enums import XPos, YPos

# Permite rodar tanto `python tools/generate_install_pdf.py` quanto
# `python -m tools.generate_install_pdf`: garante o repo root no sys.path
# antes do import absoluto de `tools.*`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Reaproveita classe-base e paleta do gerador de ajuda (mesma linguagem visual)
from tools.generate_help_pdf import (  # noqa: E402
    ACCENT,
    DIVIDER,
    SUCCESS,
    SURFACE,
    TEXT_PRI,
    TEXT_SEC,
    WARNING,
    Doc,
)

DEFAULT_OUTPUT = (
    Path(__file__).parent.parent
    / "dist"
    / f"portal-pedidos-instalacao-{date.today():%Y%m%d}.pdf"
)


class InstallDoc(Doc):
    """Mesma identidade visual, com rotulo de header proprio."""

    def header(self):
        if self.page_no() == 1:
            return
        self.set_fill_color(*SURFACE)
        self.rect(0, 0, 210, 12, "F")
        self.set_text_color(*TEXT_SEC)
        self.set_font("Helvetica", size=7)
        self.set_xy(14, 4)
        self.cell(0, 4, "Portal de Pedidos - Instalacao no Servidor", align="L")
        self.set_xy(0, 4)
        self.cell(196, 4, f"SamFlowsAI * {date.today():%d/%m/%Y}", align="R")


def code_box(pdf: InstallDoc, text: str):
    """Bloco monoespacado para comandos/enderecos."""
    pdf.ln(1)
    lines = text.strip("\n").split("\n")
    pdf.set_font("Courier", size=8.5)
    h = len(lines) * 5 + 4
    y = pdf.get_y()
    pdf.set_fill_color(*SURFACE)
    pdf.set_draw_color(*DIVIDER)
    pdf.set_line_width(0.2)
    pdf.rect(14, y, 182, h, "FD")
    pdf.set_xy(18, y + 2)
    pdf.set_text_color(*ACCENT)
    for ln in lines:
        pdf.set_x(18)
        pdf.cell(174, 5, ln, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_y(y + h + 2)


def build(output: Path):
    pdf = InstallDoc(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(True, margin=16)
    pdf.set_margins(14, 16, 14)

    # ── CAPA ──────────────────────────────────────────────────────────────────
    pdf.add_page()
    pdf.bg()
    for i, _ in enumerate(range(15, 50, 5)):
        pdf.set_fill_color(15 + i, 17 + i, 21 + i * 2)
        pdf.rect(0, 297 - (i + 1) * 12, 210, 12, "F")
    pdf.set_fill_color(*ACCENT)
    pdf.rect(0, 0, 210, 3, "F")

    pdf.set_xy(0, 55)
    pdf.set_text_color(*TEXT_SEC)
    pdf.set_font("Helvetica", size=10)
    pdf.cell(210, 8, "SamFlowsAI", align="C")

    pdf.set_y(68)
    pdf.set_text_color(*TEXT_PRI)
    pdf.set_font("Helvetica", "B", 28)
    pdf.cell(210, 12, "Portal de Pedidos", align="C")

    pdf.set_y(83)
    pdf.set_text_color(*ACCENT)
    pdf.set_font("Helvetica", size=14)
    pdf.cell(210, 8, "Guia de Instalacao no Servidor", align="C")

    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.6)
    pdf.line(60, 96, 150, 96)

    pdf.set_y(102)
    pdf.set_text_color(*TEXT_SEC)
    pdf.set_font("Helvetica", size=10)
    pdf.cell(210, 6, "O app roda somente no servidor.", align="C")
    pdf.set_y(110)
    pdf.cell(210, 6, "As estacoes de trabalho acessam pelo navegador - nao instalam nada.", align="C")

    pdf.set_y(128)
    pdf.flow_diagram(
        [
            ("Copiar ZIP", "para o servidor"),
            ("instalar.bat", "1 vez"),
            ("setup-service", "auto-start"),
            ("Navegador", "estacoes via IP"),
        ]
    )

    idx_y = 170
    pdf.set_fill_color(*SURFACE)
    pdf.rect(30, idx_y, 150, 82, "F")
    pdf.set_draw_color(*DIVIDER)
    pdf.set_line_width(0.2)
    pdf.rect(30, idx_y, 150, 82, "D")
    pdf.set_xy(30, idx_y + 4)
    pdf.set_text_color(*ACCENT)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(150, 6, "Conteudo", align="C")
    items = [
        ("01", "Pre-requisitos"),
        ("02", "Instalacao (primeira vez)"),
        ("03", "Deixar no ar automaticamente"),
        ("04", "Acesso pelas estacoes"),
        ("05", "Atualizacao"),
        ("06", "Operacao do dia a dia"),
        ("07", "Solucao de problemas"),
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

    pdf.set_y(258)
    pdf.set_text_color(*TEXT_SEC)
    pdf.set_font("Helvetica", size=8)
    pdf.cell(210, 5, f"Versao 1.0  *  {date.today():%d/%m/%Y}", align="C")
    pdf.set_fill_color(*ACCENT)
    pdf.rect(0, 294, 210, 3, "F")

    # ── 01 PRE-REQUISITOS ──────────────────────────────────────────────────────
    pdf.add_page()
    pdf.bg()
    pdf.set_y(16)
    pdf.section_title("01", "Pre-requisitos (servidor)")
    pdf.bullet("Windows 10 / 11 ou Windows Server (2016 ou superior).")
    pdf.bullet(
        "Conexao com a internet DURANTE a instalacao (para baixar o Python e as "
        "dependencias). Depois de instalado, funciona offline na rede local."
    )
    pdf.bullet(
        "Recomendado: IP fixo no servidor (estatico no Windows ou reserva de DHCP "
        "no roteador), para que o endereco de acesso nunca mude."
    )
    pdf.note_box(
        "i",
        "Nao precisa instalar Python manualmente. O instalador detecta e, se faltar, "
        "instala o Python 3.11 automaticamente (via winget).",
        color=ACCENT,
    )

    # ── 02 INSTALACAO ──────────────────────────────────────────────────────────
    pdf.section_title("02", "Instalacao (primeira vez)")
    pdf.step(1, "Copie o portal-pedidos-AAAAMMDD.zip para o servidor (ex.: C:\\PortalPedidos\\).")
    pdf.step(2, "Botao direito no .zip > Extrair tudo.")
    pdf.step(3, "Entre na pasta extraida e de duplo-clique em instalar.bat.")
    pdf.step(4, "Responda as perguntas:")
    pdf.bullet("OPENROUTER_API_KEY - chave para o processamento de PDFs complexos.", level=2)
    pdf.bullet("Modo de exportacao - 1 (xlsx) e o recomendado para comecar.", level=2)
    pdf.bullet("Porta - Enter para usar 3636.", level=2)
    pdf.bullet(
        "Acesso pela rede - escolha 1 (Rede local) para que as outras maquinas "
        "acessem pelo IP. Mesmo assim o Portal nao fica exposto na internet.",
        level=2,
    )
    pdf.bullet("Usuario admin - e-mail e senha de acesso ao Portal.", level=2)
    pdf.step(5, "No final, anote o endereco mostrado, algo como http://192.168.x.x:3636.")

    # ── 03 SERVICO ─────────────────────────────────────────────────────────────
    pdf.section_title("03", "Deixar no ar automaticamente")
    pdf.body(
        "Para o Portal subir sozinho sempre que o servidor ligar/reiniciar, sem precisar "
        "de ninguem logado:"
    )
    pdf.step(1, "De duplo-clique em setup-service.bat.")
    pdf.step(2, "Clique Sim no aviso de Administrador (UAC).")
    pdf.body("Isso registra uma Tarefa Agendada que:")
    pdf.bullet("inicia no boot do Windows;")
    pdf.bullet("roda invisivel, em segundo plano;")
    pdf.bullet("reinicia sozinha se cair.")
    pdf.note_box(
        "!",
        "Com o servico ativo, voce NAO precisa abrir o iniciar.bat. Ele so serve para "
        "subir manualmente em testes.",
        color=WARNING,
    )

    # ── 04 ACESSO ──────────────────────────────────────────────────────────────
    pdf.add_page()
    pdf.bg()
    pdf.set_y(16)
    pdf.section_title("04", "Acesso pelas estacoes de trabalho")
    pdf.body("Em qualquer PC ou celular na mesma rede, abra o navegador em:")
    code_box(pdf, "http://<IP-DO-SERVIDOR>:3636\n(ex.: http://192.168.1.50:3636)")
    pdf.body("Faca login com o usuario admin criado durante a instalacao.")

    # ── 05 ATUALIZACAO ─────────────────────────────────────────────────────────
    pdf.section_title("05", "Atualizacao (enviar uma nova versao)")
    pdf.body("Quando voce receber um novo pacote portal-pedidos-AAAAMMDD.zip:")
    pdf.step(1, "Extraia o novo .zip por cima da pasta atual, substituindo os arquivos.")
    pdf.bullet(
        "O .env (configuracoes/senhas) e os dados (usuarios, historico) NAO sao "
        "tocados - eles nao vem no pacote.",
        level=2,
    )
    pdf.step(2, "De duplo-clique em atualizar.bat (para o servico, atualiza e reinicia).")
    pdf.note_box(
        "ok",
        "Nada de reconfigurar: usuarios, chave e configuracoes permanecem.",
        color=SUCCESS,
    )

    # ── 06 OPERACAO ────────────────────────────────────────────────────────────
    pdf.section_title("06", "Operacao do dia a dia")
    pdf.table(
        ["Acao", "Como"],
        [
            ["Ver se o servico esta rodando", "Get-ScheduledTask -TaskName PortalPedidos | Select State"],
            ["Parar / iniciar manualmente", "Stop-ScheduledTask / Start-ScheduledTask -TaskName PortalPedidos"],
            ["Subir na mao (sem servico)", "duplo-clique em iniciar.bat"],
            ["Remover o auto-start", "desinstalar.bat (como Administrador)"],
        ],
        col_widths=[62, 120],
    )

    # ── 07 PROBLEMAS ───────────────────────────────────────────────────────────
    pdf.add_page()
    pdf.bg()
    pdf.set_y(16)
    pdf.section_title("07", "Solucao de problemas")
    pdf.sub_title("Outra maquina nao acessa pelo IP")
    pdf.body(
        "Confirme que escolheu 'Rede local' na instalacao (no .env deve constar "
        "PORTAL_HOST=0.0.0.0). Rode instalar.bat de novo se precisar recriar a regra de firewall."
    )
    pdf.sub_title("O endereco mudou")
    pdf.body("O servidor esta com IP dinamico. Configure um IP fixo (estatico ou reserva de DHCP).")
    pdf.sub_title("Porta 3636 ocupada")
    pdf.body("Edite PORTAL_PORT no .env para outra porta e rode atualizar.bat (ou reinicie o servico).")
    pdf.sub_title("Esqueci a senha do admin / criar outro usuario")
    pdf.body("No servidor, dentro da pasta:")
    code_box(pdf, ".venv\\Scripts\\python.exe tools\\create_user.py email@exemplo.com --role admin")

    # ── RESUMO ─────────────────────────────────────────────────────────────────
    pdf.section_title("--", "Resumo rapido")
    code_box(
        pdf,
        "Servidor (1 vez):   instalar.bat  >  setup-service.bat (Admin)\n"
        "Estacoes:           navegador > http://<IP-do-servidor>:3636\n"
        "Atualizar:          extrair novo zip por cima  >  atualizar.bat",
    )

    pdf.set_y(265)
    pdf.set_text_color(*TEXT_SEC)
    pdf.set_font("Helvetica", size=8)
    pdf.cell(
        0,
        5,
        "Portal de Pedidos  *  SamFlowsAI  *  Suporte: samucaalves@gmail.com",
        align="C",
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output))
    print(f"PDF gerado: {output}")


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    build(out)
