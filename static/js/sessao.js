/* Logout automático após 15 minutos sem uso (só com alguém logado).
   - Uso = clique, tecla, rolagem, toque ou movimento do mouse, em qualquer aba
     (a hora do último uso é compartilhada pelo localStorage).
   - Durante o uso, avisa o servidor em /sessao/ativa (no máximo a cada 5 min),
     para quem passa muito tempo numa mesma tela não perder a sessão ao salvar.
   - Acabado o tempo, sai por /logout?inatividade=1 e volta para o login (área
     restrita) ou para a própria página (painéis públicos).
   O servidor também encerra a sessão por conta própria (area_restrita.py). */
(function () {
  var logado = document.cookie.split(';').some(function (c) { return c.trim() === 'painel_sessao=1'; });
  if (!logado) return;

  var LIMITE = 15 * 60 * 1000;   // 15 minutos sem uso
  var AVISO = 5 * 60 * 1000;     // intervalo mínimo entre avisos ao servidor
  var CHAVE = 'painel_ultima_atividade';
  var ultimoUso = Date.now();
  var ultimoAviso = Date.now();

  function lerCompartilhado() {
    try { return Number(localStorage.getItem(CHAVE)) || 0; } catch (e) { return 0; }
  }

  function registrarUso() {
    var agora = Date.now();
    if (agora - ultimoUso < 5000) return;  // movimentos seguidos contam uma vez a cada 5 s
    ultimoUso = agora;
    try { localStorage.setItem(CHAVE, String(agora)); } catch (e) { /* sem localStorage: vale só esta aba */ }
    if (agora - ultimoAviso >= AVISO) {
      ultimoAviso = agora;
      fetch('/sessao/ativa', { credentials: 'same-origin', cache: 'no-store' }).catch(function () {});
    }
  }

  ['click', 'keydown', 'scroll', 'touchstart', 'mousemove', 'wheel'].forEach(function (evento) {
    window.addEventListener(evento, registrarUso, { passive: true });
  });
  try { localStorage.setItem(CHAVE, String(ultimoUso)); } catch (e) { /* idem */ }

  setInterval(function () {
    var ultimo = Math.max(ultimoUso, lerCompartilhado());
    if (Date.now() - ultimo >= LIMITE) {
      window.location.href = '/logout?inatividade=1&proximo=' +
        encodeURIComponent(window.location.pathname + window.location.search);
    }
  }, 30000);
})();
