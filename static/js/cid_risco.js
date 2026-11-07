// Tabela de atendimentos por CID (linhas) x classificação de risco (colunas), da
// página da Emergência e da Tendência Mensal. Cada página tem o seu corRisco().
function preencherCidRisco(tabela, resumo, m) {
  const fmt = n => Number(n).toLocaleString('pt-BR');
  const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  if (!m || m.linhas.length === 0) {
    resumo.textContent = '';
    tabela.querySelector('thead').innerHTML = '<tr><th>CID</th></tr>';
    tabela.querySelector('tbody').innerHTML = '<tr><td>Nenhum CID disponível no período.</td></tr>';
    return;
  }
  const semCid = m.linhas.filter(l => l.cid === 'Sem CID').reduce((s, l) => s + l.total, 0);
  const qtdCids = m.linhas.length - (semCid ? 1 : 0);
  const comCid = m.total - semCid;
  resumo.innerHTML = `${fmt(qtdCids)} CIDs no período &nbsp;|&nbsp; ` +
    `Atendimentos com CID: <b>${(comCid / m.total * 100).toLocaleString('pt-BR', { maximumFractionDigits: 1 })}%</b> ` +
    `&nbsp;(${fmt(comCid)} de ${fmt(m.total)})`;
  tabela.querySelector('thead').innerHTML = '<tr><th>CID</th>' +
    m.riscos.map(r => `<th class="num"><span class="risco-dot" style="background:${corRisco(r)}"></span>${esc(r)}</th>`).join('') +
    '<th class="num">Total</th><th class="num">%</th></tr>';
  const pct = v => (v / m.total * 100).toLocaleString('pt-BR', { maximumFractionDigits: 1 }) + '%';
  tabela.querySelector('tbody').innerHTML = m.linhas.map(l => `
    <tr>
      <td>${esc(l.cid)}</td>
      ${l.valores.map(v => `<td class="num">${v ? fmt(v) : '—'}</td>`).join('')}
      <td class="num"><b>${fmt(l.total)}</b></td>
      <td class="num">${pct(l.total)}</td>
    </tr>`).join('') +
    '<tr class="total"><td>Total</td>' + m.totais.map(v => `<td class="num">${fmt(v)}</td>`).join('') +
    `<td class="num">${fmt(m.total)}</td><td class="num">100%</td></tr>`;
}
