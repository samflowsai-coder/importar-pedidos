/**
 * roteamento-labels.js — vocabulário do roteamento intercompany.
 *
 * Compartilhado entre a faixa de roteamento do preview (index.html) e a tela
 * de treinamento (admin-roteamento.html) — as duas precisam traduzir o mesmo
 * degrau técnico ('documento'/'historico'/'memoria') para o rótulo que o
 * operador aprende. Duplicar este dicionário nas duas páginas foi o que
 * deixou "escolha registrada" (faixa) e "memoria" (tabela) divergentes.
 */
const ROUTING_DEGRAU_LABEL = {
  documento: 'pelo documento',
  historico: 'pelo histórico',
  memoria: 'escolha registrada',
};
