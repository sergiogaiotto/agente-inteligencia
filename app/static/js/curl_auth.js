/* ════════════════════════════════════════════════════════════════════════
   curl_auth.js — "estação de autenticação" reusável para os modais de cURL.

   Problema que resolve: o snippet de invoke mostrava `X-API-Key: SUA_API_KEY`
   literal — pra rodar, o usuário tinha que sair pra Configurações, criar uma
   chave, copiar o plaintext (que só aparece UMA vez) e voltar pra colar na mão.

   Restrição dura do backend: a plataforma guarda só o HASH da chave
   (app/core/auth_apikey.py) — o GET /api/v1/api-keys devolve prefixo, NUNCA o
   segredo. Logo, "reusar uma chave existente" e colá-la pronta é impossível: o
   único instante em que o plaintext existe é o da criação. Por isso o modo
   recomendado é "Gerar e embutir" (cria a chave agora e injeta no comando).

   Três módulos:
   - buildInvokeCurl(): fonte ÚNICA do escaping por shell (Bash/PowerShell/CMD)
     + injeção da chave. Antes vivia inline em mesh_flow.html (duplicação).
   - maskApiKey(): mascara o segredo na TELA (prefixo + ••• + últimos 4) — copia-se
     o valor real, mas a tela não vaza a chave em prints/compartilhamento.
   - curlAuthStation(): factory de estado Alpine; espalhe com `...curlAuthStation()`
     no componente da página. A marcação fica em partials/curl_auth_modal.html.

   Depende de globais definidos em base.html: `api`, `showToast`, `window.copyText`.
   ════════════════════════════════════════════════════════════════════════ */

/* Raiz agnóstica de runtime: `window` no browser, `globalThis` sob node (teste). */
var _root = (typeof window !== 'undefined') ? window : (typeof globalThis !== 'undefined' ? globalThis : this);

/* Monta o cURL do invoke por SHELL: muda a continuação de linha e o escaping do
   body JSON. `bodyKey` permite trocar a chave do payload ('message' no invoke de
   agente/pipeline; 'input' no execute do Catálogo). `apiKey` ausente → placeholder. */
_root.buildInvokeCurl = function ({ url, message, shell, apiKey, bodyKey } = {}) {
    const key = apiKey || 'SUA_API_KEY';
    const obj = {};
    obj[bodyKey || 'message'] = message || '';
    const json = JSON.stringify(obj);                 // {"message":"..."} ou {"input":"..."}
    if (shell === 'powershell') {
        // PowerShell: curl.exe (não o alias Invoke-WebRequest); continuação = backtick;
        // aspas simples literais → escapa ' como '' dentro de '...'.
        const body = json.replace(/'/g, "''");
        const bt = '`';
        return "curl.exe -X POST '" + url + "' " + bt + "\n" +
               "  -H 'Content-Type: application/json' " + bt + "\n" +
               "  -H 'X-API-Key: " + key + "' " + bt + "\n" +
               "  -d '" + body + "'";
    }
    if (shell === 'cmd') {
        // CMD: continuação = ^; body em aspas duplas com as aspas do JSON escapadas (\").
        const body = json.replace(/"/g, '\\"');
        return 'curl -X POST "' + url + '" ^\n' +
               '  -H "Content-Type: application/json" ^\n' +
               '  -H "X-API-Key: ' + key + '" ^\n' +
               '  -d "' + body + '"';
    }
    // Bash (Linux/macOS/Git Bash): continuação = \\; body em '...' com ' → '\'' .
    const body = json.replace(/'/g, "'\\''");
    return "curl -X POST '" + url + "' \\\n" +
           "  -H 'Content-Type: application/json' \\\n" +
           "  -H 'X-API-Key: " + key + "' \\\n" +
           "  -d '" + body + "'";
};

/* Mascara a chave na tela mantendo o prefixo de 12 chars (o mesmo mostrado na
   lista de Configurações, pra correlação) + os últimos 4. Curtas demais → intactas. */
_root.maskApiKey = function (key) {
    if (!key) return '';
    if (key.length <= 16) return key;
    return key.slice(0, 12) + '••••••••' + key.slice(-4);
};

_root.CURL_AUTH_SHELLS = [
    { k: 'bash', l: 'Bash (Linux/macOS)' },
    { k: 'powershell', l: 'PowerShell' },
    { k: 'cmd', l: 'CMD (Windows)' },
];
_root.CURL_AUTH_EXPIRY = [
    { v: '90', l: '90 dias' },
    { v: '30', l: '30 dias' },
    { v: '180', l: '180 dias' },
    { v: '0', l: 'Nunca' },
];

/* Factory de estado Alpine. Espalhe no componente da página:
     function minhaPagina() { return { ...curlAuthStation(), ...resto }; }
   e inclua o modal: {% include 'partials/curl_auth_modal.html' %} dentro do x-data. */
_root.curlAuthStation = function () {
    return {
        CURL_AUTH_SHELLS: _root.CURL_AUTH_SHELLS,
        CURL_AUTH_EXPIRY: _root.CURL_AUTH_EXPIRY,
        curlAuth: {
            open: false,
            mode: 'embed',            // 'embed' | 'existing' | 'placeholder'
            shell: 'bash',
            message: 'sua entrada aqui',
            url: '',                  // endpoint de invoke (setado pelo opener)
            title: 'cURL do invoke',
            keyNameHint: 'cURL',      // base do nome da chave gerada
            bodyKey: 'message',       // 'message' (invoke) | 'input' (catálogo)
            note: '',                 // rodapé contextual (ex.: async do catálogo)
            expiry: '90',             // dias; '0' = nunca
            generatedKey: null,       // plaintext recém-gerado (modo embed) — só agora
            reveal: false,            // mostra o segredo na tela?
            existingKeys: [],         // chaves ativas do user (modo existing)
            selectedPrefix: '',       // prefixo da chave existente escolhida
            pastedKey: '',            // chave que o user colou (modo existing)
            creating: false,
        },

        openCurlAuth(opts = {}) {
            this.curlAuth = {
                ...this.curlAuth,
                mode: 'embed', shell: 'bash', generatedKey: null, reveal: false,
                selectedPrefix: '', pastedKey: '', expiry: '90', note: '',
                ...opts,
                open: true,
            };
            this._loadExistingKeys();
        },
        closeCurlAuth() { this.curlAuth.open = false; this.curlAuth.generatedKey = null; },

        async _loadExistingKeys() {
            try {
                const d = await api.get('/api/v1/api-keys');
                this.curlAuth.existingKeys = (d.keys || []).filter(k => k.active);
            } catch { this.curlAuth.existingKeys = []; }
        },

        /* Chave "real" a usar no comando (pra cópia). null em embed antes de gerar.
           MÉTODO (não getter): a página compõe com `{ ...curlAuthStation() }`, e o
           spread de objeto AVALIA getters uma vez e congela o valor — quebrando a
           reatividade (shell/mensagem/chave parariam de atualizar a tela). Funções
           sobrevivem ao spread por referência e reavaliam a cada render. */
        curlAuthRealKey() {
            const a = this.curlAuth;
            if (a.mode === 'embed') return a.generatedKey || null;
            if (a.mode === 'existing') return a.pastedKey || (a.selectedPrefix ? a.selectedPrefix + '…' : null);
            return 'SUA_API_KEY';
        },
        /* Há um segredo de verdade embutido? (controla avisos + máscara) */
        curlAuthHasSecret() {
            const a = this.curlAuth;
            return (a.mode === 'embed' && !!a.generatedKey) || (a.mode === 'existing' && !!a.pastedKey);
        },
        /* Comando MOSTRADO na tela: mascara o segredo a menos que `reveal`.
           x-text="curlAuthCommand()" reavalia quando shell/mensagem/chave mudam. */
        curlAuthCommand() {
            const a = this.curlAuth;
            let shownKey;
            if (this.curlAuthHasSecret() && !a.reveal) {
                shownKey = window.maskApiKey(a.mode === 'embed' ? a.generatedKey : a.pastedKey);
            } else {
                shownKey = this.curlAuthRealKey() || 'SUA_API_KEY';
            }
            return window.buildInvokeCurl({ url: a.url, message: a.message, shell: a.shell, apiKey: shownKey, bodyKey: a.bodyKey });
        },

        /* Modo recomendado: cria a chave AGORA (único instante do plaintext) e embute. */
        async generateAndEmbed() {
            const a = this.curlAuth;
            a.creating = true;
            try {
                const today = new Date().toISOString().slice(0, 10);
                const payload = { name: a.keyNameHint + ' · ' + today };
                if (a.expiry && a.expiry !== '0') {
                    const d = new Date();
                    d.setDate(d.getDate() + parseInt(a.expiry, 10));
                    payload.expires_at = d.toISOString();
                }
                const r = await api.post('/api/v1/api-keys', payload);
                a.generatedKey = r.key;
                a.reveal = false;
                showToast('Chave gerada e embutida — aparece só agora', 'success');
            } catch (e) {
                showToast('Erro ao gerar chave: ' + (e?.message || ''), 'error');
            }
            a.creating = false;
        },

        /* Copia o comando com o segredo REAL (não o mascarado da tela). */
        async copyCurlAuth() {
            const a = this.curlAuth;
            const cmd = window.buildInvokeCurl({ url: a.url, message: a.message, shell: a.shell, apiKey: this.curlAuthRealKey(), bodyKey: a.bodyKey });
            if (await window.copyText(cmd)) {
                if (this.curlAuthHasSecret()) showToast('Comando copiado — contém um segredo, trate como senha', 'info');
                else showToast('cURL copiado (' + a.shell + ')', 'success');
            } else {
                showToast('Não consegui copiar — selecione e Ctrl+C', 'error');
            }
        },
    };
};

/* Sob node (teste), as funções ficam em globalThis após o require — ver
   tests que carregam este arquivo e leem globalThis.buildInvokeCurl/maskApiKey. */
