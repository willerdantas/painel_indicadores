/*
 * seguranca.js: carregado em todas as páginas pelo recursos() do _layout.html,
 * antes dos scripts da página.
 *
 * escHtml(texto): escapa texto vindo do banco (nomes de clínica, especialidade,
 * CID, cadastros...) antes de entrar em HTML montado com innerHTML.
 *
 * Rede de proteção: todo HTML posto com innerHTML / insertAdjacentHTML em
 * elementos HTML é lido antes num <template> (inerte: nada executa nem
 * carrega) e perde o que roda código: <script>, <iframe>, <object>, <embed>,
 * atributos on* (onclick, onerror...) e links javascript:. Por isso o HTML
 * montado nas páginas não usa onclick; os eventos vão por addEventListener.
 */
(function () {
  'use strict';

  const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  window.escHtml = s => String(s ?? '').replace(/[&<>"']/g, c => ESCAPES[c]);

  const PROIBIDOS = 'script, iframe, frame, object, embed, base, meta, link';
  const ATRIBUTOS_URL = ['href', 'src', 'action', 'formaction', 'xlink:href'];
  const URL_PERIGOSA = /^\s*(javascript|vbscript|data:text\/html)/i;
  const HTML_NS = 'http://www.w3.org/1999/xhtml';

  function limpar(raiz) {
    raiz.querySelectorAll(PROIBIDOS).forEach(el => el.remove());
    raiz.querySelectorAll('*').forEach(el => {
      for (const at of Array.from(el.attributes)) {
        const nome = at.name.toLowerCase();
        if (nome.startsWith('on') || (ATRIBUTOS_URL.includes(nome) && URL_PERIGOSA.test(at.value))) {
          el.removeAttribute(at.name);
        }
      }
    });
  }

  const original = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML');
  const adjacenteOriginal = Element.prototype.insertAdjacentHTML;
  if (!original || !original.set) return;

  function fragmentoLimpo(html) {
    const tpl = document.createElement('template');
    original.set.call(tpl, String(html));
    limpar(tpl.content);
    return tpl.content;
  }

  // só elementos HTML comuns: SVG e o próprio <template> seguem o caminho normal
  const filtra = el => el.namespaceURI === HTML_NS && el.localName !== 'template';

  Object.defineProperty(Element.prototype, 'innerHTML', {
    configurable: true,
    enumerable: original.enumerable,
    get: original.get,
    set(html) {
      if (!filtra(this) || !/[<&]/.test(String(html))) return original.set.call(this, html);
      this.replaceChildren(fragmentoLimpo(html));
    },
  });

  Element.prototype.insertAdjacentHTML = function (posicao, html) {
    if (!filtra(this)) return adjacenteOriginal.call(this, posicao, html);
    const frag = fragmentoLimpo(html);
    switch (String(posicao).toLowerCase()) {
      case 'beforebegin': this.before(frag); break;
      case 'afterbegin': this.prepend(frag); break;
      case 'beforeend': this.append(frag); break;
      case 'afterend': this.after(frag); break;
      default: adjacenteOriginal.call(this, posicao, '');
    }
  };
})();
