// Tabela de internações e saídas por CID (linhas) e destino da saída (colunas),
// da página Hospitalares e da Tendência Mensal; 10 linhas visíveis (tabela_rolagem.js).
function preencherCidDestino(caixa, resumo, m) {
  const fmt = n => Number(n).toLocaleString('pt-BR');
  const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const pct = (q, total) => (q / total * 100).toLocaleString('pt-BR', { maximumFractionDigits: 1 }) + '%';
  const destinos = m ? m.destinos : [];
  const linhas = m ? m.linhas : [];
  caixa.querySelector('thead').innerHTML = '<tr><th>CID</th><th class="num">Internações</th>' +
    destinos.map(d => `<th class="num" title="Saídas com este destino">${esc(d)}</th>`).join('') +
    '<th class="num">Total de saídas</th></tr>';
  if (linhas.length === 0) {
    resumo.textContent = '';
    caixa.querySelector('tbody').innerHTML = `<tr><td colspan="${destinos.length + 3}">Nenhum CID disponível no período.</td></tr>`;
    limitarLinhasVisiveis(caixa, 10);
    return;
  }
  const sem = linhas.find(l => l.cid === 'Sem CID');
  const qtdCids = linhas.length - (sem ? 1 : 0);
  const comCid = (total, semCid, nome) => total
    ? `${nome} com CID: <b>${pct(total - semCid, total)}</b> &nbsp;(${fmt(total - semCid)} de ${fmt(total)})` : `${nome}: nenhuma`;
  resumo.innerHTML = `${fmt(qtdCids)} CIDs no período &nbsp;|&nbsp; ` +
    comCid(m.totais.internacoes, sem ? sem.internacoes : 0, 'Internações') + ' &nbsp;|&nbsp; ' +
    comCid(m.totais.total, sem ? sem.total : 0, 'Saídas');
  const qtd = v => v ? fmt(v) : '—';
  const linha = (nome, l, classe) => `<tr class="${classe || ''}"><td>${esc(nome)}</td>
      <td class="num">${qtd(l.internacoes)}</td>
      ${l.saidas.map(v => `<td class="num">${qtd(v)}</td>`).join('')}
      <td class="num"><b>${qtd(l.total)}</b></td></tr>`;
  caixa.querySelector('tbody').innerHTML = linhas.map(l => linha(l.cid, l)).join('') + linha('Total', m.totais, 'total');
  limitarLinhasVisiveis(caixa, 10);
}
