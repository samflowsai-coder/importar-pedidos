/**
 * shell.js — Renders the persistent sidebar + top bar.
 *
 * Each page that wants the shell must include:
 *   <link rel="stylesheet" href="/static/css/tokens.css?v=1">
 *   <link rel="stylesheet" href="/static/css/shell.css?v=1">
 *   <script src="/static/js/shell.js?v=1" defer></script>
 *   <div id="app-shell"></div>     (anywhere in <body>)
 *
 * The shell:
 *  - fetches /api/auth/me ONCE and caches in window.__shellUser
 *  - hides "Configurações" group for non-admin users
 *  - polls /api/config every 30s to drive the Firebird status indicator
 *  - exposes window.appShell.{showError, showSuccess, currentUser, fb}
 *  - sets <html data-shell-ready> after first render to unhide the shell
 *
 * Page-local code (e.g. index.html's giant inline script) can read
 * window.__shellUser instead of re-calling /api/auth/me.
 */
(function () {
  'use strict';

  const ICONS = {
    logo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 12l9 4 9-4"/><path d="M3 17l9 4 9-4"/></svg>',
    pedidos: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 4h6a2 2 0 0 1 2 2v1h1a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h1V6a2 2 0 0 1 2-2z"/><path d="M9 12h6M9 16h4"/></svg>',
    settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    db: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.66 3.58 3 8 3s8-1.34 8-3V5"/><path d="M4 11v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6"/></svg>',
    folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>',
    users: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
    chevron: '<svg class="app-nav-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>',
    logout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>',
  };

  const NAV_DEF = {
    pedidos: { label: 'Pedidos', href: '/', icon: ICONS.pedidos, route: 'pedidos' },
    config:  { label: 'Configurações', icon: ICONS.settings, route: 'config', adminOnly: true,
      children: [
        { label: 'Ambientes',      href: '/admin/ambientes',         icon: ICONS.db, route: 'admin-ambientes' },
        { label: 'Diretórios',     href: '/configuracoes/diretorios', icon: ICONS.folder, route: 'config-diretorios' },
        { label: 'Usuários',       href: '/configuracoes/usuarios',   icon: ICONS.users, route: 'config-usuarios' },
      ],
    },
  };

  const ACTIVE_BY_PATH = {
    '/':                          'pedidos',
    '/admin/ambientes':           'admin-ambientes',
    '/admin/ambientes/novo':      'admin-ambientes',
    '/configuracoes/diretorios':  'config-diretorios',
    '/configuracoes/usuarios':    'config-usuarios',
  };

  function el(html) {
    const t = document.createElement('template');
    t.innerHTML = html.trim();
    return t.content.firstChild;
  }

  function escape(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
  }

  async function fetchMe() {
    if (window.__shellUser !== undefined) return window.__shellUser;
    try {
      const r = await fetch('/api/auth/me', { credentials: 'same-origin' });
      const data = await r.json();
      window.__shellUser = data.user || null;
    } catch (_) {
      window.__shellUser = null;
    }
    return window.__shellUser;
  }

  async function fetchConfig() {
    try {
      const r = await fetch('/api/config', { credentials: 'same-origin' });
      if (!r.ok) return null;
      return await r.json();
    } catch (_) { return null; }
  }

  function activeRoute() {
    return ACTIVE_BY_PATH[location.pathname] || null;
  }

  function renderSidebar(user) {
    const active = activeRoute();
    const isAdmin = user && user.role === 'admin';
    const showConfig = isAdmin;

    const links = [];
    // Pedidos
    links.push(`
      <a class="app-nav-link ${active === 'pedidos' ? 'active' : ''}"
         href="${NAV_DEF.pedidos.href}" data-route="pedidos">
        ${NAV_DEF.pedidos.icon}<span>${NAV_DEF.pedidos.label}</span>
      </a>`);

    if (showConfig) {
      const expanded = ['admin-ambientes', 'config-diretorios', 'config-usuarios'].includes(active);
      const childLinks = NAV_DEF.config.children.map((c) => `
        <a class="app-nav-link ${active === c.route ? 'active' : ''}"
           href="${c.href}" data-route="${c.route}">
          ${c.icon}<span>${c.label}</span>
        </a>`).join('');
      links.push(`
        <div class="app-nav-group" data-open="${expanded}">
          <div class="app-nav-link" data-toggle="config">
            ${NAV_DEF.config.icon}<span>${NAV_DEF.config.label}</span>${ICONS.chevron}
          </div>
          <div class="app-nav-children">${childLinks}</div>
        </div>`);
    }

    return `
      <aside class="app-shell">
        <div class="app-shell-brand">
          <div class="app-shell-logo">${ICONS.logo}</div>
          <span class="app-shell-name">Portal de Pedidos</span>
        </div>
        <nav class="app-shell-nav">
          <div class="app-nav-section">Geral</div>
          ${links.join('')}
        </nav>
        <div class="app-shell-foot">
          <span>v0.1.0</span>
          <span title="Ambiente">local</span>
        </div>
      </aside>`;
  }

  function userInitials(email) {
    if (!email) return '?';
    const at = email.split('@')[0];
    return at.slice(0, 2).toUpperCase();
  }

  function renderTopbar(user) {
    if (!user) return '';
    return `
      <div class="app-shell-top">
        <div class="app-shell-top-left" data-shell-title></div>
        <div class="app-shell-top-right">
          <div class="app-shell-fb" data-fb-status title="Status do Firebird">
            <span class="app-shell-fb-dot"></span>
            <span data-fb-label>Firebird</span>
          </div>
          <div class="app-shell-user" title="${escape(user.email)}">
            <span class="app-shell-avatar">${userInitials(user.email)}</span>
            <span>${escape(user.role || '')}</span>
          </div>
          <button class="app-shell-icon-btn danger" data-shell-logout title="Sair">
            ${ICONS.logout}
          </button>
        </div>
      </div>`;
  }

  function bindEvents(host) {
    host.querySelectorAll('[data-toggle="config"]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const grp = btn.closest('.app-nav-group');
        grp.dataset.open = grp.dataset.open === 'true' ? 'false' : 'true';
      });
    });

    const logout = host.querySelector('[data-shell-logout]');
    if (logout) {
      logout.addEventListener('click', async () => {
        await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
        location.href = '/login';
      });
    }
  }

  async function refreshFbStatus(host) {
    const cfg = await fetchConfig();
    const node = host.querySelector('[data-fb-status]');
    if (!node) return;
    const ok = !!(cfg && cfg.firebirdConfigured);
    node.classList.toggle('connected', ok);
    const label = node.querySelector('[data-fb-label]');
    if (label) label.textContent = ok ? 'Conectado' : 'Sem banco';
  }

  function ensureToastHost() {
    let host = document.querySelector('.app-shell-toasts');
    if (!host) {
      host = document.createElement('div');
      host.className = 'app-shell-toasts';
      document.body.appendChild(host);
    }
    return host;
  }

  function showToast({ message, kind = 'info', traceId = null, ttl = 5000 }) {
    const host = ensureToastHost();
    const node = el(`
      <div class="app-shell-toast ${kind}">
        <div class="app-shell-toast-msg">${escape(message)}</div>
        ${traceId ? `
          <div class="app-shell-toast-trace">
            <span>trace ${escape(traceId)}</span>
            <button data-copy>copiar</button>
          </div>` : ''}
      </div>`);
    host.appendChild(node);
    if (traceId) {
      node.querySelector('[data-copy]').addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(traceId); } catch (_) {}
      });
    }
    if (ttl) setTimeout(() => node.remove(), ttl);
    return node;
  }

  async function mount() {
    const slot = document.getElementById('app-shell');
    if (!slot) return;
    document.body.classList.add('has-app-shell');

    const user = await fetchMe();
    if (!user) {
      // Anonymous on a shell-protected page → redirect to login.
      // (Pages still render their own auth gates if they need finer control.)
      if (location.pathname !== '/login') {
        location.href = '/login';
        return;
      }
    }

    slot.innerHTML = renderSidebar(user) + renderTopbar(user);
    bindEvents(slot);
    refreshFbStatus(slot);
    setInterval(() => refreshFbStatus(slot), 30000);

    document.documentElement.setAttribute('data-shell-ready', '1');
  }

  // Public API
  window.appShell = {
    showError(message, traceId)   { return showToast({ message, kind: 'error',   traceId, ttl: 0 }); },
    showSuccess(message)          { return showToast({ message, kind: 'success', ttl: 3500 }); },
    showInfo(message)             { return showToast({ message, kind: 'info',    ttl: 3500 }); },
    currentUser() { return window.__shellUser || null; },
    refreshFb()   { const slot = document.getElementById('app-shell'); if (slot) refreshFbStatus(slot); },
    setTitle(text) {
      const t = document.querySelector('[data-shell-title]');
      if (t) t.textContent = text || '';
    },
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
