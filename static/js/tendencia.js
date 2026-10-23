/* Funções comuns dos gráficos de tendência mensal (Indicadores Hospitalares e
   da Emergência): números, média e mediana, linhas de média/mediana e os
   valores escritos em cada barra/ponto. Carregar depois do Chart.js. */

// número no formato brasileiro (até 2 casas)
const fmtNum = n => (n === null || n === undefined || isNaN(n)) ? '—'
  : Number(n).toLocaleString('pt-BR', { maximumFractionDigits: 2 });

// valor escrito no gráfico: menos casas quanto maior o número, para caber
function fmtRotulo(n) {
  const a = Math.abs(n);
  const casas = a >= 100 ? 0 : (a >= 10 ? 1 : 2);
  return Number(n).toLocaleString('pt-BR', { maximumFractionDigits: casas });
}

const numeros = vals => (vals || []).map(v => (v === null || v === undefined) ? null : parseFloat(v));

function calcularMedia(valores) {
  const nums = valores.map(v => parseFloat(v)).filter(v => !isNaN(v));
  return nums.length > 0 ? parseFloat((nums.reduce((a, b) => a + b, 0) / nums.length).toFixed(2)) : null;
}

function calcularMediana(valores) {
  const nums = valores.map(v => parseFloat(v)).filter(v => !isNaN(v)).sort((a, b) => a - b);
  if (nums.length === 0) return null;
  const mid = Math.floor(nums.length / 2);
  return parseFloat((nums.length % 2 ? nums[mid] : (nums[mid - 1] + nums[mid]) / 2).toFixed(2));
}

// linhas horizontais de média e mediana (paleta: laranja e azul); o valor de
// cada uma fica na legenda, não é escrito ponto a ponto (rotulo: false)
function linhasMediaMediana(qtdMeses, media, mediana, sufixo, eixo) {
  const s = sufixo || '';
  const base = { type: 'line', backgroundColor: 'transparent', borderWidth: 2.5, pointRadius: 0,
                 pointHoverRadius: 0, tension: 0, fill: false, order: 0, rotulo: false, sufixo: s };
  if (eixo) base.yAxisID = eixo;
  return [
    { ...base, label: `Média: ${fmtNum(media)}${s}`, data: media !== null ? Array(qtdMeses).fill(media) : [],
      borderColor: '#EB600A', borderDash: [6, 4] },
    { ...base, label: `Mediana: ${fmtNum(mediana)}${s}`, data: mediana !== null ? Array(qtdMeses).fill(mediana) : [],
      borderColor: '#2E758C', borderDash: [2, 3] }
  ];
}

// Escreve o valor de cada mês: acima da barra/ponto ou, nas barras empilhadas,
// dentro de cada faixa (só quando a faixa é alta e larga o bastante).
// Datasets com rotulo: false (média/mediana) ficam de fora; sufixo: ex. '%'.
const rotulosValores = {
  id: 'rotulosValores',
  afterDatasetsDraw(chart) {
    const ctx = chart.ctx;
    const empilhado = !!(chart.options.scales && chart.options.scales.x && chart.options.scales.x.stacked);
    ctx.save();
    ctx.textAlign = 'center';
    chart.data.datasets.forEach((ds, i) => {
      if (ds.rotulo === false || !chart.isDatasetVisible(i)) return;
      const meta = chart.getDatasetMeta(i);
      const tamanho = meta.data.length > 24 ? 9 : 11;
      const dentro = empilhado && meta.type === 'bar';
      ctx.font = `600 ${tamanho}px "DM Sans", sans-serif`;
      meta.data.forEach((el, j) => {
        const v = ds.data[j];
        if (v === null || v === undefined || isNaN(v) || (dentro && !v)) return;
        const texto = fmtRotulo(v) + (ds.sufixo || '');
        if (dentro) {
          const altura = Math.abs(el.base - el.y);
          if (altura < tamanho + 4 || ctx.measureText(texto).width > el.width - 2) return;
          ctx.fillStyle = '#fff';
          ctx.textBaseline = 'middle';
          ctx.fillText(texto, el.x, (el.y + el.base) / 2);
        } else {
          // contorno branco: o número continua legível sobre as linhas de média/mediana
          const y = el.y - (meta.type === 'line' ? 6 : 3);
          ctx.textBaseline = 'bottom';
          ctx.lineWidth = 3;
          ctx.lineJoin = 'round';
          ctx.strokeStyle = 'rgba(255, 255, 255, .9)';
          ctx.strokeText(texto, el.x, y);
          ctx.fillStyle = meta.type === 'line' ? (ds.borderColor || '#38464D') : '#38464D';
          ctx.fillText(texto, el.x, y);
        }
      });
    });
    ctx.restore();
  }
};
Chart.register(rotulosValores);
// espaço no topo para o valor da barra mais alta não ser cortado
Chart.defaults.layout.padding = { top: 22 };
