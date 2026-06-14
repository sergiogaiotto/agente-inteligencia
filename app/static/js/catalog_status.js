/* ════════════════════════════════════════════════════════════════════════
   catalog_status.js — rótulos pt-BR dos status do Catálogo (display-only).

   Os valores reais ficam em INGLÊS no DB/API (draft, submitted, ...). Estes
   helpers só traduzem o texto MOSTRADO; nunca troque os value= dos <option>
   nem as comparações (entry.status === 'published') — só o que o usuário lê.

   - catalogStatusLabel(): status de lifecycle da entry.
   - catalogReviewLabel():  status de revisão de uma submissão.

   Carregado no base.html (head, antes do Alpine defer), igual ao curl_auth.js.
   Sob node (teste), as funções ficam em globalThis após o require.
   ════════════════════════════════════════════════════════════════════════ */
var _csRoot = (typeof window !== 'undefined') ? window : (typeof globalThis !== 'undefined' ? globalThis : this);

_csRoot.catalogStatusLabel = function (s) {
    return ({
        draft: 'Rascunho', submitted: 'Em revisão', approved: 'Aprovada',
        published: 'Publicada', deprecated: 'Depreciada', archived: 'Arquivada',
    })[s] || s;
};

_csRoot.catalogReviewLabel = function (s) {
    return ({
        pending: 'Pendente', approved: 'Aprovada', rejected: 'Rejeitada',
        changes_requested: 'Mudanças solicitadas',
    })[s] || s;
};
