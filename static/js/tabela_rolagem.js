// Tabela com rolagem que mostra N linhas, com cabeçalho e linha de total fixos
// (CSS: .tabela-rolagem-fixa). A altura da caixa = cabeçalho + as N primeiras
// linhas + total; com N linhas ou menos (ex.: com filtro), fica sem rolagem.
// Linhas escondidas (grupos recolhidos) não contam: chame de novo ao abrir/fechar.
function limitarLinhasVisiveis(caixa, n) {
  const linhas = Array.from(caixa.querySelectorAll('tbody tr:not(.total)')).filter(tr => tr.getClientRects().length);
  if (linhas.length <= n) { caixa.style.maxHeight = ''; return; }
  const altura = el => el ? el.getBoundingClientRect().height : 0;
  let total = altura(caixa.querySelector('thead')) + altura(caixa.querySelector('tr.total'));
  for (let i = 0; i < n; i++) total += altura(linhas[i]);
  caixa.style.maxHeight = Math.ceil(total) + 2 + 'px';
}
