// Internações por procedência (t_internacao.isn_referencia): barras empilhadas
// por procedência com a linha do total, e a tabela abaixo do gráfico; da página
// Hospitalares (por dia) e da Tendência Mensal (por mês). p vem de
// indicadores_hcasg._por_procedencia: {series: [{nome, valores, total}], total, outras}.
const CORES_PROCEDENCIA = ['#2E758C', '#00B2C9', '#F3A133', '#3BB5A3', '#EB600A', '#53B7D9', '#0F9688'];
const CORES_PROCEDENCIA_FIXAS = { 'Outras': '#9AA5AB', 'Não informada': '#CFD6D9' };

function corProcedencia(nome, idx) {
  return CORES_PROCEDENCIA_FIXAS[nome] || CORES_PROCEDENCIA[idx % CORES_PROCEDENCIA.length];
}

// configuração do Chart.js: rotulos são os dias ou meses do eixo x
function configProcedencia(rotulos, p) {
  const fmt = n => Number(n || 0).toLocaleString('pt-BR');
  const muitos = rotulos.length > 62;
  const totalGeral = p.total.reduce((a, v) => a + v, 0);
  return {
    type: 'bar',
    data: {
      labels: rotulos,
      datasets: [
        ...p.series.map((s, idx) => {
          const cor = corProcedencia(s.nome, idx);
          return {
            label: `${s.nome} (${fmt(s.total)})`, data: s.valores, stack: 'procedencia', order: 2,
            backgroundColor: cor + 'cc', borderColor: cor, borderWidth: 1, borderRadius: 2
          };
        }),
        {
          // total do dia/mês, fora da pilha
          label: `Total (${fmt(totalGeral)})`, type: 'line', data: p.total, stack: 'total', order: 0,
          borderColor: '#38464D', backgroundColor: 'transparent', borderWidth: 2.5,
          pointRadius: muitos ? 0 : 3, pointHoverRadius: 4, tension: 0.2, fill: false
        }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { intersect: false, mode: 'index' },
      plugins: {
        legend: { display: true, position: 'bottom', labels: { usePointStyle: true, padding: 15 } },
        tooltip: { callbacks: { label: c => ` ${c.dataset.label.replace(/ \([\d.]+\)$/, '')}: ${fmt(c.parsed.y)}` } }
      },
      scales: {
        x: { stacked: true, ticks: { font: { size: 11 }, maxRotation: 45, autoSkip: true, maxTicksLimit: 31 } },
        y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } }
      }
    }
  };
}

// tabela: cada procedência do gráfico (as de "Outras" logo abaixo dela), total,
// % e média por dia/mês (rotuloMedia); 5 linhas visíveis (tabela_rolagem.js)
function tabelaProcedencia(caixa, p, rotuloMedia) {
  const fmt = n => Number(n).toLocaleString('pt-BR');
  const dec = n => n.toLocaleString('pt-BR', { maximumFractionDigits: 1 });
  const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const periodos = p.total.length;
  const totalGeral = p.total.reduce((a, v) => a + v, 0);
  caixa.querySelector('thead').innerHTML = `<tr><th>Procedência</th><th class="num">Total no período</th>
    <th class="num">%</th><th class="num">${rotuloMedia}</th></tr>`;
  const linha = (nome, total, cor, classe) => `<tr class="${classe || ''}">
      <td>${cor ? `<span class="marca-cor" style="background:${cor}"></span>` : ''}${esc(nome)}</td>
      <td class="num">${fmt(total)}</td>
      <td class="num">${totalGeral ? dec(Math.round(total / totalGeral * 1000) / 10) + '%' : '—'}</td>
      <td class="num">${periodos ? dec(Math.round(total / periodos * 10) / 10) : '—'}</td></tr>`;
  caixa.querySelector('tbody').innerHTML = p.series.length
    ? p.series.map((s, idx) => linha(s.nome, s.total, corProcedencia(s.nome, idx)) +
        (s.nome === 'Outras' ? p.outras.map(o => linha('↳ ' + o.nome, o.total, null, 'sub')).join('') : '')).join('') +
      linha('Total', totalGeral, null, 'total')
    : '<tr><td colspan="4">Nenhuma internação no período.</td></tr>';
  limitarLinhasVisiveis(caixa, 5);
}
