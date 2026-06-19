window.adminConsole = function adminConsole(config) {
  return {
    uiPath: config.uiPath,
    loggedInEmail: config.loggedInEmail,
    navItems: [
      { key: "dashboard", label: "Dashboard", icon: "📊" },
      { key: "tenants", label: "Tenants", icon: "🏢" },
      { key: "users", label: "Credentials", icon: "🔑" },
      { key: "approvals", label: "Approvals", icon: "✅" },
      { key: "servers", label: "MCP Servers", icon: "🧰" },
      { key: "catalog", label: "Catalog", icon: "🗂️" },
      { key: "usage", label: "Usage & Quota", icon: "📒" },
      { key: "telemetry", label: "Telemetry", icon: "📈" },
      { key: "embeddings", label: "Embeddings", icon: "🧠" },
      { key: "account", label: "Account", icon: "🧑" },
    ],
    activeSection: "dashboard",
    forms: {
      newTenantId: "",
      user: {
        email: "",
        password: "",
        role: "tenant-admin",
        scopes: "",
        status: "active",
      },
      passwordChange: {
        current_password: "",
        new_password: "",
      },
      server: {
        server: "",
        transport: "code",
        endpoint: "",
        command: "",
        metadata: '{"domain":"custom","runtime":"wasm"}',
        tools: [
          {
            local_id: 1,
            name: "",
            description: "",
            action_type: "read",
            requires_confirmation: false,
            requirements: "",
            raw_code: "",
            scopes: "",
            input_schema: "{}",
            test_arguments: "{}",
            expanded: true,
          },
        ],
      },
      search: {
        query: "",
        mode: "hybrid",
        limit: 10,
      },
      serverEnv: {
        key: "",
        value: "",
      },
      catalog: {
        query: "",
      },
      tenantConnect: {
        allowlist: "",
      },
      embedding: {
        provider: "ollama",
        model: "",
        base_url: "",
        api_key: "",
        azure_endpoint: "",
        azure_api_version: "",
        azure_deployment: "",
        reprovision: true,
      },
      tenantEmbedding: {
        provider: "ollama",
        model: "",
        base_url: "",
        api_key: "",
        azure_endpoint: "",
        azure_api_version: "",
        azure_deployment: "",
        reprovision: true,
      },
    },
    embeddingProviders: [
      { value: "ollama", label: "Ollama (local)" },
      { value: "openai", label: "OpenAI" },
      { value: "azure_openai", label: "Azure OpenAI" },
      { value: "voyage", label: "Voyage AI" },
      { value: "gemini", label: "Google Gemini" },
    ],
    state: {
      theme: "dark",
      tenantId: config.defaultTenantId,
      tenantOptions: [config.defaultTenantId],
      whoami: null,
      stats: null,
      tenants: [],
      users: [],
      pendingActions: [],
      userNotice: "",
      servers: [],
      exportingServer: false,
      catalog: { items: [] },
      catalogExpanded: {},
      telemetry: { items: [] },
      // Server-side analytics rollups (gateway/routers/admin/analytics.py). Scope
      // is derived by the server from the caller's role; the console requests
      // platform scope for a platform-admin and tenant scope otherwise.
      analytics: {
        overview: null,
        usageTrend: null,
        topTools: null,
        telemetryTrend: null,
        quota: null,
      },
      analyticsLoading: false,
      // Smoothly-tweened mirror of the headline KPIs so the numbers "count up"
      // when a section loads / the tenant switches (purely cosmetic).
      displayMetrics: { calls: 0, sandbox: 0 },
      // Tenants table client-side filter: "all" or "unconfirmed" (the beta queue).
      tenantFilter: "all",
      // Usage & quota management view.
      usage: null,
      usageEvents: { events: [], totals_by_kind: {}, total_amount: 0 },
      usageLoading: false,
      quotaForm: { calls_limit: 0, sandbox_seconds_limit: 0 },
      quotaSaving: false,
      searchResults: [],
      embeddingScope: "platform",
      embedding: null,
      embeddingStatus: null,
      embeddingTest: null,
      embeddingSaving: false,
      tenantEmbedding: null,
      tenantEmbeddingStatus: null,
      tenantEmbeddingTest: null,
      tenantEmbeddingSaving: false,
      errorMessage: "",
      toasts: [],
      helpOpen: false,
      helpTab: "mcp",
      // "Legal & data" summary modal (links out to the full Terms / Privacy pages).
      legalOpen: false,
      contextHelpOpen: false,
      contextHelpTab: "overview",
      toolTestResults: {},
      toolValidation: {},
      tenantConnectOpen: false,
      tenantConnectTenant: "",
      tenantConnectEgress: null,
      tenantConnectSaving: false,
      userTokenOpen: false,
      userTokenUser: "",
      userTokenResult: null,
      userTokenGenerating: false,
      // One-time password shown in the token modal right after a demo account is
      // created (it is never retrievable again). Cleared when the modal closes.
      userTokenNewPassword: "",
      // Optional human-friendly label applied to the next credential we mint, and
      // the MCP client whose config we show (drives the connect-flow snippet).
      credName: "",
      connectClient: "cursor",
      demoCreating: false,
      viewerCreating: false,
      teamCreating: false,
      // Advanced/custom user form lives behind a disclosure so the one-click
      // persona cards are the obvious default path.
      userAdvancedOpen: false,
      // Per-tenant tool policy editor (allowlist + max-tools + disabled overlay).
      toolPolicyOpen: false,
      toolPolicyTenant: "",
      toolPolicy: null,
      toolPolicyAllowlist: [],
      toolPolicyMaxTools: 0,
      toolPolicyLoading: false,
      toolPolicySaving: false,
      serverComposerOpen: false,
      serverComposerMode: "create",
      serverWorkspace: {
        open: false,
        server: "",
        tab: "tools",
      },
      serverEnv: {
        keys: [],
        updated_at: null,
        updated_by: null,
      },
      serverEnvLoading: false,
      serverEnvSaving: false,
      exploreCollections: [],
      exploreCollectionsTenant: "",
      exploreCollectionsLoading: false,
      workspaceExplore: null,
      workspaceExploreTargetId: null,
      toolPaletteFilter: "",
    },
    _embeddingPoll: null,
    _tenantEmbeddingPoll: null,
    _toastSeq: 0,
    _toolSeq: 1,
    _codeEditors: {},
    // Live Chart.js instances keyed by canvas id, so every render destroys the
    // prior chart before recreating it (Chart.js leaks/overlays otherwise on a
    // refresh or tenant switch).
    _charts: {},
    // requestAnimationFrame handles for the KPI count-up tweens, keyed by metric
    // so a new tween (e.g. on tenant switch) cancels the one in flight.
    _metricTweens: {},
    _validateTimers: {},
    // Soft, advisory-only cap: pinning many tools spends the agent's context
    // budget on every call. Not enforced server-side -- the admin decides.
    recommendedAlwaysIncludedMax: 5,

    async init() {
      this.initTheme();
      this._initHashRouting();
      try {
        await this.loadWhoAmI();
        // The tenant picker needs the tenant list regardless of the landing
        // section, so always load it; then hydrate whatever section the URL
        // hash (deep link) or default resolves to.
        await this.loadTenants();
        await this.refreshActiveSection();
      } catch (_) {
        // requests already set error/redirect when needed
      }
    },

    // ---- Hash-based deep linking ------------------------------------------- //
    // The active tab is mirrored into location.hash (#tenants, #telemetry, ...)
    // so a section is bookmarkable / shareable and survives a reload, and the
    // browser back/forward buttons move between tabs.
    _sectionFromHash() {
      const raw = String(window.location.hash || "")
        .replace(/^#/, "")
        .trim();
      if (!raw) return null;
      return this.navItems.some((item) => item.key === raw) ? raw : null;
    },

    _initHashRouting() {
      const fromHash = this._sectionFromHash();
      if (fromHash) this.activeSection = fromHash;
      window.addEventListener("hashchange", () => {
        const section = this._sectionFromHash();
        if (section && section !== this.activeSection) {
          this.activeSection = section;
          this.refreshActiveSection();
        }
      });
    },

    async apiRequest(path, options = {}) {
      const method = options.method || "GET";
      const body = options.body || null;
      const includeTenant = options.includeTenant !== false;
      const url = new URL(path, window.location.origin);
      if (includeTenant && this.state.tenantId) {
        url.searchParams.set("tenant_id", this.state.tenantId);
      }
      const headers = {
        Accept: "application/json",
        ...(options.headers || {}),
      };
      if (body !== null) {
        headers["Content-Type"] = "application/json";
      }
      // The CSRF token must accompany every state-changing request, not only
      // those carrying a JSON body: a bodyless DELETE (e.g. deleting a user)
      // still mutates state and is rejected by the server's CSRF check if the
      // header is missing.
      const unsafeMethod = ["POST", "PUT", "PATCH", "DELETE"].includes(
        method.toUpperCase(),
      );
      if (unsafeMethod) {
        const csrfToken = this.readCookie(config.csrfCookieName);
        if (csrfToken) {
          headers["X-CSRF-Token"] = csrfToken;
        }
      }

      const response = await fetch(url.toString(), {
        method,
        headers,
        credentials: "same-origin",
        body: body === null ? undefined : JSON.stringify(body),
      });
      if (response.status === 401) {
        window.location.href = `${this.uiPath}/login`;
        throw new Error("Authentication required");
      }
      if (!response.ok) {
        let detail = `${response.status} ${response.statusText}`;
        try {
          const payload = await response.json();
          if (payload.detail) {
            detail = payload.detail;
          }
        } catch (_) {
          // ignore parse failure
        }
        throw new Error(detail);
      }
      if (response.status === 204) {
        return {};
      }
      return response.json();
    },

    readCookie(name) {
      const match = document.cookie
        .split("; ")
        .find((row) => row.startsWith(`${name}=`));
      return match ? decodeURIComponent(match.split("=")[1]) : "";
    },

    setError(error) {
      const message =
        error instanceof Error ? error.message : String(error || "");
      this.state.errorMessage = message;
      if (message) {
        this.pushToast(message, "error", 6500);
      }
    },

    clearError() {
      this.state.errorMessage = "";
    },

    pushToast(message, type = "info", timeout = 4200) {
      const text = String(message || "").trim();
      if (!text) return null;
      const id = ++this._toastSeq;
      const icons = { success: "✓", error: "!", warning: "⚠", info: "›" };
      this.state.toasts.push({
        id,
        message: text,
        type,
        icon: icons[type] || icons.info,
      });
      if (timeout > 0) {
        setTimeout(() => this.dismissToast(id), timeout);
      }
      return id;
    },

    dismissToast(id) {
      this.state.toasts = this.state.toasts.filter((toast) => toast.id !== id);
    },

    notify(message, type = "success") {
      return this.pushToast(message, type);
    },

    // ---- "Learn MCP" help modal -------------------------------------------- //
    openHelp(tab = "mcp") {
      if (tab) this.state.helpTab = tab;
      this.state.helpOpen = true;
    },

    closeHelp() {
      this.state.helpOpen = false;
    },

    // ---- "The magic of context" guide ------------------------------------- //
    openContextHelp(tab = "overview") {
      if (tab) this.state.contextHelpTab = tab;
      this.state.contextHelpOpen = true;
    },

    closeContextHelp() {
      this.state.contextHelpOpen = false;
    },

    async openTenantConnect(tenantId) {
      this.state.tenantConnectTenant = String(
        tenantId || this.state.tenantId || "",
      ).trim();
      this.state.tenantConnectOpen = true;
      await this.loadTenantEgressAllowlist(this.state.tenantConnectTenant);
    },

    closeTenantConnect() {
      this.state.tenantConnectOpen = false;
      this.state.tenantConnectSaving = false;
    },

    async generateUserToken(user, options = {}) {
      this.clearError();
      this.state.userTokenGenerating = true;
      // Carry a one-time password only when the caller (createDemoUser) just
      // minted the account; a plain "Get config" never exposes a password.
      this.state.userTokenNewPassword = options.password || "";
      // Default the connect modal to the client this credential was minted for,
      // so reopening "Get config" lands on the right config without re-picking.
      if (this.connectClients.some((c) => c.key === user?.client)) {
        this.state.connectClient = user.client;
      }
      try {
        const result = await this.apiRequest(
          `/admin/users/${encodeURIComponent(user.id)}/token`,
          { method: "POST", body: {} },
        );
        this.state.userTokenResult = result;
        this.state.userTokenUser = user.email;
        this.state.userTokenOpen = true;
        this.notify(`Token generated for '${user.email}'.`);
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.userTokenGenerating = false;
      }
    },

    // Body shared by every one-click tier: the active tenant plus the optional
    // cosmetic label/client the operator picked above the cards. Empty values are
    // omitted so the request stays clean.
    _credCreateBody() {
      const body = { tenant_id: this.state.tenantId };
      const label = (this.state.credName || "").trim();
      if (label) body.label = label;
      if (this.state.connectClient) body.client = this.state.connectClient;
      return body;
    },

    async createDemoUser() {
      this.clearError();
      this.state.userNotice = "";
      this.state.demoCreating = true;
      try {
        const result = await this.apiRequest("/admin/users/demo", {
          method: "POST",
          body: this._credCreateBody(),
        });
        await this.loadUsers();
        this.state.credName = "";
        this.notify(`Full-access credential '${this.credentialName(result.user)}' created.`);
        // Immediately hand over a working credential: open the token modal with
        // the freshly minted bearer plus the one-time password.
        await this.generateUserToken(result.user, { password: result.password });
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.demoCreating = false;
      }
    },

    async createTeamUser() {
      this.clearError();
      this.state.userNotice = "";
      this.state.teamCreating = true;
      try {
        const result = await this.apiRequest("/admin/users/team", {
          method: "POST",
          body: this._credCreateBody(),
        });
        await this.loadUsers();
        this.state.credName = "";
        this.notify(`Read-only credential '${this.credentialName(result.user)}' created.`);
        // Same hand-off as the full-access button: open the token modal with a
        // working bearer + one-time password, ready to paste into a client config.
        await this.generateUserToken(result.user, { password: result.password });
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.teamCreating = false;
      }
    },

    closeUserToken() {
      this.state.userTokenOpen = false;
      this.state.userTokenResult = null;
      this.state.userTokenUser = "";
      this.state.userTokenNewPassword = "";
    },

    mcpEndpoint() {
      // The mounted FastMCP sub-app requires the trailing slash.
      return `${window.location.origin}/mcp/`;
    },

    // The MCP clients we generate paste-ready config for. Cursor and Claude
    // Desktop share the `mcpServers` url+headers shape; VS Code uses its own
    // `servers` + `type: "http"` schema. Order = how they appear in the picker.
    connectClients: [
      { key: "cursor", label: "Cursor", file: "~/.cursor/mcp.json", cap: "mcp.json" },
      {
        key: "claude",
        label: "Claude Desktop",
        file: "claude_desktop_config.json",
        cap: "claude_desktop_config.json",
      },
      { key: "vscode", label: "VS Code", file: ".vscode/mcp.json", cap: ".vscode/mcp.json" },
    ],

    clientConfigMeta() {
      const key = this.state.connectClient || "cursor";
      return (
        this.connectClients.find((c) => c.key === key) || this.connectClients[0]
      );
    },

    // Paste-ready config for the selected client, with the live bearer baked in.
    // The token is identical across clients; only the wrapper schema differs.
    clientConfigSnippet() {
      const result = this.state.userTokenResult;
      if (!result) return "";
      const token = result.token || "";
      const url = this.mcpEndpoint();
      if ((this.state.connectClient || "cursor") === "vscode") {
        return [
          "{",
          '  "servers": {',
          '    "mdb-mcp-gateway": {',
          '      "type": "http",',
          `      "url": "${url}",`,
          '      "headers": {',
          `        "Authorization": "Bearer ${token}"`,
          "      }",
          "    }",
          "  }",
          "}",
        ].join("\n");
      }
      // Cursor + Claude Desktop both consume the `mcpServers` url/headers shape.
      return [
        "{",
        '  "mcpServers": {',
        '    "mdb-mcp-gateway": {',
        `      "url": "${url}",`,
        '      "headers": {',
        `        "Authorization": "Bearer ${token}"`,
        "      }",
        "    }",
        "  }",
        "}",
      ].join("\n");
    },

    userCurlSample() {
      const result = this.state.userTokenResult;
      if (!result) return "";
      return [
        "curl -X POST " + this.rpcEndpoint() + " \\",
        '  -H "Content-Type: application/json" \\',
        `  -H "Authorization: Bearer ${result.token || ""}" \\`,
        "  -d '{\"jsonrpc\":\"2.0\",\"id\":\"list-1\",\"method\":\"tools/list\",\"params\":{}}'",
      ].join("\n");
    },

    rpcEndpoint() {
      return `${window.location.origin}/rpc`;
    },

    authModeLabel() {
      return this.state.whoami?.auth_mode || "hs256";
    },

    tokenEndpoint() {
      return `${window.location.origin}/auth/token`;
    },

    passwordTokenCommand() {
      return [
        "# Exchange a username/password for a bearer (OAuth2 password grant):",
        "curl -X POST " + this.tokenEndpoint() + " \\",
        '  -H "Content-Type: application/x-www-form-urlencoded" \\',
        "  -d 'grant_type=password&username=$EMAIL&password=$PASSWORD'",
        "",
        '# Response: {"access_token":"...","token_type":"bearer","expires_in":...}',
        "# Then call the gateway with: Authorization: Bearer <access_token>",
      ].join("\n");
    },

    rpcCurlSample() {
      const tenantId =
        this.state.tenantConnectTenant || this.state.tenantId || "local-dev";
      return [
        "curl -X POST " + this.rpcEndpoint() + " \\",
        '  -H "Content-Type: application/json" \\',
        '  -H "Authorization: Bearer $TOKEN" \\',
        "  -d '{",
        '    \"jsonrpc\":\"2.0\",',
        '    \"id\":\"call-1\",',
        '    \"method\":\"tools/call\",',
        '    \"params\":{',
        '      \"server\":\"weather\",',
        '      \"name\":\"get_current_weather\",',
        '      \"arguments\":{\"city\":\"Lisbon\",\"unit\":\"celsius\"}',
        "    }",
        "  }'",
        "",
        `# token must contain tenant_id=${tenantId}, roles:[\"tool:invoke\"], groups/scopes`,
      ].join("\n");
    },

    mcpClientSnippet() {
      return [
        "{",
        '  \"mcpServers\": {',
        '    \"gateway\": {',
        '      \"transport\": \"streamable_http\",',
        `      \"url\": \"${this.rpcEndpoint()}\",`,
        '      \"headers\": {',
        '        \"Authorization\": \"Bearer ${TOKEN}\"',
        "      }",
        "    }",
        "  }",
        "}",
      ].join("\n");
    },

    async loadTenantEgressAllowlist(tenantId) {
      if (!tenantId) return;
      try {
        const payload = await this.apiRequest(
          `/admin/tenants/${encodeURIComponent(tenantId)}/egress-allowlist`,
          { includeTenant: false },
        );
        this.state.tenantConnectEgress = payload;
        this.forms.tenantConnect.allowlist = (payload.allowlist || []).join(
          "\n",
        );
      } catch (error) {
        this.state.tenantConnectEgress = null;
        this.forms.tenantConnect.allowlist = "";
        this.setError(error);
      }
    },

    async saveTenantEgressAllowlist() {
      const tenantId = this.state.tenantConnectTenant;
      if (!tenantId) return;
      const allowlist = String(this.forms.tenantConnect.allowlist || "")
        .split(/\n|,/)
        .map((item) => item.trim())
        .filter(Boolean);
      this.state.tenantConnectSaving = true;
      this.clearError();
      try {
        const payload = await this.apiRequest(
          `/admin/tenants/${encodeURIComponent(tenantId)}/egress-allowlist`,
          {
            method: "PUT",
            includeTenant: false,
            body: { allowlist },
          },
        );
        this.state.tenantConnectEgress = payload;
        this.forms.tenantConnect.allowlist = (payload.allowlist || []).join(
          "\n",
        );
        this.notify(`Updated egress allowlist for '${tenantId}'.`);
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.tenantConnectSaving = false;
      }
    },

    async copyText(text, label = "Snippet", ev = null) {
      const value = String(text || "").trim();
      if (!value) return;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(value);
        } else {
          // Fallback for non-secure contexts where the async clipboard API is
          // unavailable.
          const helper = document.createElement("textarea");
          helper.value = value;
          helper.style.position = "fixed";
          helper.style.opacity = "0";
          document.body.appendChild(helper);
          helper.select();
          document.execCommand("copy");
          document.body.removeChild(helper);
        }
        this.notify(`${label} copied to clipboard.`, "success");
        this.flashCopied(ev);
      } catch (error) {
        this.setError(`Copy failed: ${error.message || error}`);
      }
    },

    // Inline affordance: briefly swap the clicked copy button to a "Copied ✓"
    // state so the action feels tactile even if the toast is missed. No-op when
    // invoked without an event (programmatic copies). Restores the original
    // label after the timeout and guards against re-entrancy on rapid clicks.
    flashCopied(ev) {
      const btn = ev && ev.currentTarget;
      if (!btn || btn.dataset.copying === "1") return;
      const original = btn.innerHTML;
      btn.dataset.copying = "1";
      btn.classList.add("is-copied");
      btn.innerHTML = "Copied ✓";
      window.setTimeout(() => {
        btn.innerHTML = original;
        btn.classList.remove("is-copied");
        delete btn.dataset.copying;
      }, 1400);
    },

    // Dashboard "Connect Now": a ready-to-paste Cursor mcp.json with the live
    // gateway endpoint baked in and a ${TOKEN} placeholder, so an operator can
    // grab it before (or instead of) minting a token in the Users tab.
    quickConnectMcp() {
      return [
        "{",
        '  "mcpServers": {',
        '    "mdb-mcp-gateway": {',
        `      "url": "${this.mcpEndpoint()}",`,
        '      "headers": {',
        '        "Authorization": "Bearer ${TOKEN}"',
        "      }",
        "    }",
        "  }",
        "}",
      ].join("\n");
    },

    initTheme() {
      const stored = window.localStorage.getItem("gateway-admin-theme");
      if (stored === "light" || stored === "dark") {
        this.state.theme = stored;
        return;
      }
      // MongoDB-branded dark is the default experience; users can still opt into
      // light, and that choice is remembered.
      this.state.theme = "dark";
    },

    toggleTheme() {
      this.state.theme = this.state.theme === "dark" ? "light" : "dark";
      window.localStorage.setItem("gateway-admin-theme", this.state.theme);
    },

    isDarkTheme() {
      return this.state.theme === "dark";
    },

    formatDate(raw) {
      if (!raw) return "-";
      const parsed = new Date(raw);
      if (Number.isNaN(parsed.getTime())) return String(raw);
      return parsed.toLocaleString();
    },

    formatJson(raw) {
      if (!raw || typeof raw !== "object") return String(raw || "{}");
      return JSON.stringify(raw);
    },

    checklistDone(step) {
      if (step === "tenant") return (this.state.tenants || []).length > 0;
      if (step === "function") {
        const rows = this.state.stats?.tenants || [];
        return rows.some((row) => Number(row.tool_count || 0) > 0);
      }
      if (step === "connect") {
        return Boolean(this.state.stats?.catalog_version);
      }
      return false;
    },

    checklistCountCompleted() {
      return ["tenant", "function", "connect"].filter((step) =>
        this.checklistDone(step),
      ).length;
    },

    goToChecklistStep(step) {
      if (step === "tenant") this.switchSection("tenants");
      if (step === "function") this.switchSection("servers");
      if (step === "connect") {
        this.switchSection("servers");
        if (this.state.serverWorkspace.open) {
          this.setWorkspaceTab("search");
        } else if ((this.state.servers || []).length > 0) {
          this.openServerWorkspace(this.state.servers[0], "search");
        }
      }
    },

    switchSection(section) {
      this.activeSection = section;
      // Mirror into the URL hash (guarded so we don't loop with the hashchange
      // listener, which only reacts when the hash names a *different* section).
      if (this._sectionFromHash() !== section) {
        window.location.hash = section;
      }
      this.refreshActiveSection();
    },

    async refreshActiveSection() {
      this.clearError();
      if (
        this.state.exploreCollectionsTenant &&
        this.state.exploreCollectionsTenant !== this.state.tenantId
      ) {
        this.state.exploreCollectionsTenant = "";
        this.state.exploreCollections = [];
      }
      try {
        if (this.activeSection === "dashboard") {
          await this.loadStats();
          await this.loadAnalytics();
        }
        if (this.activeSection === "tenants") await this.loadTenants();
        if (this.activeSection === "users") await this.loadUsers();
        if (this.activeSection === "catalog") await this.loadCatalog();
        if (this.activeSection === "usage") await this.loadUsageView();
        if (this.activeSection === "approvals") await this.loadPendingActions();
        if (this.activeSection === "servers") {
          await this.loadServers();
          if (
            this.state.serverWorkspace.open &&
            String(this.state.serverWorkspace.server || "").trim()
          ) {
            const selected = this.state.servers.find(
              (item) => String(item.server || "") === String(this.state.serverWorkspace.server || ""),
            );
            if (!selected) {
              this.closeServerWorkspace();
            }
          }
          if (this.state.serverComposerOpen) {
            this.$nextTick(() => this.refreshCodeEditors());
          } else {
            this._teardownCodeEditors();
          }
        }
        if (this.activeSection === "telemetry") await this.loadTelemetry();
        if (this.activeSection === "embeddings") {
          // One section, two scopes: load both so the toggle is instant.
          await this.loadEmbedding();
          await this.loadTenantEmbedding();
        }
      } catch (error) {
        this.setError(error);
      }
    },

    async loadWhoAmI() {
      try {
        this.state.whoami = await this.apiRequest("/admin/whoami", {
          includeTenant: false,
        });
      } catch (error) {
        this.setError(error);
        throw error;
      }
    },

    async loadStats() {
      this.clearError();
      try {
        this.state.stats = await this.apiRequest("/admin/stats", {
          includeTenant: false,
        });
      } catch (error) {
        this.setError(error);
      }
    },

    async loadTenants() {
      this.clearError();
      try {
        this.state.tenants = await this.apiRequest("/admin/tenants", {
          includeTenant: false,
        });
        const ids = this.state.tenants.map((t) => t.tenant_id);
        if (ids.length > 0) {
          this.state.tenantOptions = ids;
          if (!ids.includes(this.state.tenantId)) {
            this.state.tenantId = ids[0];
          }
        }
      } catch (error) {
        this.setError(error);
      }
    },

    async createTenant() {
      this.clearError();
      const created = this.forms.newTenantId;
      try {
        await this.apiRequest("/admin/tenants", {
          method: "POST",
          includeTenant: false,
          body: { tenant_id: this.forms.newTenantId },
        });
        this.forms.newTenantId = "";
        await this.loadTenants();
        this.notify(`Tenant '${created}' created.`);
        await this.openTenantConnect(created);
      } catch (error) {
        this.setError(error);
      }
    },

    async toggleTenantStatus(tenant) {
      this.clearError();
      const suspend = tenant.status !== "suspended";
      let reason = null;
      if (suspend) {
        reason = window.prompt(
          `Suspend tenant '${tenant.tenant_id}'? Optional reason:`,
          "",
        );
        if (reason === null) return;
      }
      try {
        const action = suspend ? "suspend" : "resume";
        await this.apiRequest(
          `/admin/tenants/${encodeURIComponent(tenant.tenant_id)}/${action}`,
          {
            method: "POST",
            includeTenant: false,
            body: suspend ? { reason } : {},
          },
        );
        await this.loadTenants();
        this.notify(
          `Tenant '${tenant.tenant_id}' ${suspend ? "suspended" : "resumed"}.`,
        );
      } catch (error) {
        this.setError(error);
      }
    },

    // ---- Read-only / viewer principal UX ---------------------------------- //
    // Server-side RbacMiddleware is the real guard (every mutating /admin call
    // 403s for a viewer); this only hides affordances so the console feels honest.
    canMutate() {
      return !this.state.whoami?.is_read_only;
    },

    isPlatformAdmin() {
      return Boolean(this.state.whoami?.is_platform_admin);
    },

    async createViewerUser() {
      this.clearError();
      this.state.userNotice = "";
      this.state.viewerCreating = true;
      try {
        const result = await this.apiRequest("/admin/users/viewer", {
          method: "POST",
          body: this._credCreateBody(),
        });
        await this.loadUsers();
        this.state.credName = "";
        this.notify(`Explore credential '${this.credentialName(result.user)}' created.`);
        // Hand over a working discover-only credential immediately (same flow as
        // the full-access button): the token modal carries the bearer + one-time password.
        await this.generateUserToken(result.user, { password: result.password });
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.viewerCreating = false;
      }
    },

    async toggleTenantReadOnly(tenant) {
      this.clearError();
      const enable = !tenant.read_only;
      let reason = null;
      if (enable) {
        reason = window.prompt(
          `Make tenant '${tenant.tenant_id}' read-only? It stays fully discoverable but all tool calls and config changes are blocked. Optional reason:`,
          "",
        );
        if (reason === null) return;
      }
      try {
        const action = enable ? "read-only" : "read-write";
        await this.apiRequest(
          `/admin/tenants/${encodeURIComponent(tenant.tenant_id)}/${action}`,
          {
            method: "POST",
            includeTenant: false,
            body: enable ? { reason } : {},
          },
        );
        await this.loadTenants();
        // The caller's own tenant flag can change the console's read-only banner.
        await this.loadWhoAmI();
        this.notify(
          `Tenant '${tenant.tenant_id}' is now ${enable ? "read-only" : "read-write"}.`,
        );
      } catch (error) {
        this.setError(error);
      }
    },

    // ---- Per-tenant tool policy (allowlist, max-tools, disabled overlay) --- //
    async openToolPolicy(tenantId) {
      this.state.toolPolicyTenant = String(tenantId || this.state.tenantId || "").trim();
      this.state.toolPolicyOpen = true;
      await this.loadToolPolicy(this.state.toolPolicyTenant);
    },

    closeToolPolicy() {
      this.state.toolPolicyOpen = false;
      this.state.toolPolicy = null;
      this.state.toolPolicyAllowlist = [];
      this.state.toolPolicySaving = false;
    },

    async loadToolPolicy(tenantId) {
      this.state.toolPolicyLoading = true;
      try {
        const payload = await this.apiRequest(
          `/admin/tenants/${encodeURIComponent(tenantId)}/tool-policy`,
          { includeTenant: false },
        );
        this.state.toolPolicy = payload;
        this.state.toolPolicyAllowlist = [...(payload.allowlist || [])];
        this.state.toolPolicyMaxTools = Number(payload.max_tools || 0);
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.toolPolicyLoading = false;
      }
    },

    toolPolicyKey(tool) {
      return `${tool.server}/${tool.name}`;
    },

    isToolAllowlisted(tool) {
      const list = this.state.toolPolicyAllowlist || [];
      return list.includes(this.toolPolicyKey(tool)) || list.includes(`${tool.server}/*`);
    },

    toggleAllowlistTool(tool) {
      const key = this.toolPolicyKey(tool);
      const list = this.state.toolPolicyAllowlist || [];
      if (list.includes(key)) {
        this.state.toolPolicyAllowlist = list.filter((entry) => entry !== key);
      } else {
        this.state.toolPolicyAllowlist = [...list, key];
      }
    },

    async saveToolPolicy() {
      const tenantId = this.state.toolPolicyTenant;
      this.state.toolPolicySaving = true;
      this.clearError();
      try {
        const payload = await this.apiRequest(
          `/admin/tenants/${encodeURIComponent(tenantId)}/tool-policy`,
          {
            method: "PUT",
            includeTenant: false,
            body: {
              allowlist: this.state.toolPolicyAllowlist || [],
              max_tools: Number(this.state.toolPolicyMaxTools || 0),
            },
          },
        );
        this.state.toolPolicy = payload;
        this.state.toolPolicyAllowlist = [...(payload.allowlist || [])];
        this.notify(`Tool policy saved for '${tenantId}'.`);
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.toolPolicySaving = false;
      }
    },

    async toggleToolEnabled(tool) {
      const tenantId = this.state.toolPolicyTenant;
      const action = tool.disabled ? "enable" : "disable";
      this.clearError();
      try {
        await this.apiRequest(
          `/admin/tools/${encodeURIComponent(tool.server)}/${encodeURIComponent(tool.name)}/${action}`,
          { method: "POST", includeTenant: false },
        );
        await this.loadToolPolicy(tenantId);
        this.notify(`Tool '${tool.server}/${tool.name}' ${action}d.`);
      } catch (error) {
        this.setError(error);
      }
    },

    async toggleServerEnabled(server) {
      this.clearError();
      const enable = !server.enabled;
      const action = enable ? "enable" : "disable";
      try {
        await this.apiRequest(
          `/admin/servers/${encodeURIComponent(server.server)}/${action}`,
          { method: "POST" },
        );
        await this.loadServers();
        this.notify(`Server '${server.server}' ${action}d.`);
      } catch (error) {
        this.setError(error);
      }
    },

    roleOptions: [
      { value: "user", label: "User (no tools)" },
      { value: "team", label: "Read-only (safe invoke)" },
      { value: "demo", label: "Full access (read + write)" },
      { value: "viewer", label: "Explore (discover-only)" },
      { value: "tenant-admin", label: "Tenant admin (console)" },
      { value: "platform-admin", label: "Platform admin (console)" },
    ],

    rolesForSelection(selection) {
      if (selection === "platform-admin") return ["platform-admin", "admin"];
      if (selection === "tenant-admin") return ["admin"];
      // A demo account can authenticate AND reach the /rpc + /mcp data plane
      // (rbac requires 'admin' or 'tool:invoke'), without any admin console access.
      if (selection === "demo") return ["user", "tool:invoke"];
      // A team member is a demo on the role axis (it can invoke) — the safety
      // comes from its scopes (read-only tools only), prefilled in onUserRoleChange.
      if (selection === "team") return ["user", "tool:invoke"];
      // A viewer is the complete read-only identity (matches the one-click "Create
      // viewer user" button): `viewer` reaches the admin console read-only (every
      // mutation 403s) and `tool:read` discovers tools over MCP without invoking.
      if (selection === "viewer") return ["user", "viewer", "tool:read"];
      return ["user"];
    },

    async onUserRoleChange() {
      // Picking an invoke persona should produce a user that can actually call
      // tools. Empty scopes silently fail discovery + invocation, so prefill the
      // catalog-derived scope set (only when the operator hasn't typed their own).
      // Demo gets every scope; team gets only the read-only (non-destructive) set.
      const role = this.forms.user.role;
      const endpoint =
        role === "demo"
          ? "demo-scopes"
          : role === "team"
            ? "safe-scopes"
            : null;
      if (!endpoint || this.forms.user.scopes.trim()) return;
      try {
        const payload = await this.apiRequest(
          `/admin/users/${endpoint}?tenant_id=${encodeURIComponent(this.state.tenantId)}`,
        );
        this.forms.user.scopes = (payload.scopes || []).join(", ");
      } catch (error) {
        this.setError(error);
      }
    },

    // Capability label for a credential/operator. Full-access and read-only both
    // carry the `tool:invoke` role, so they are told apart by scopes: a read-only
    // scope set carries no write/destructive scope (heuristically, none with
    // ":write"). Console roles (admin/platform-admin) are checked first.
    personaLabel(user) {
      const roles = new Set(user?.roles || []);
      if (roles.has("platform-admin")) return "Platform admin";
      if (roles.has("admin")) return "Tenant admin";
      if (roles.has("tool:invoke")) {
        const scopes = user?.scopes || [];
        const hasWriteScope = scopes.some(
          (s) => typeof s === "string" && s.includes(":write"),
        );
        return hasWriteScope ? "Full access" : "Read-only";
      }
      if (roles.has("tool:read") || roles.has("viewer")) return "Explore (discover-only)";
      return (user?.roles || []).join(", ") || "user";
    },

    // The "agent credentials vs console operators" split. A credential's purpose
    // is an MCP bearer (it can invoke or discover tools); a console operator is a
    // human who signs in to manage the console (admin/platform-admin). The viewer
    // tier lands under credentials: its hero use is a read-only MCP/console
    // showcase, and it carries `tool:read` for discovery over MCP.
    isAgentCredential(user) {
      const roles = new Set(user?.roles || []);
      if (roles.has("admin") || roles.has("platform-admin")) return false;
      return (
        roles.has("tool:invoke") || roles.has("tool:read") || roles.has("viewer")
      );
    },

    agentCredentials() {
      return (this.state.users || []).filter((u) => this.isAgentCredential(u));
    },

    consoleOperators() {
      return (this.state.users || []).filter((u) => !this.isAgentCredential(u));
    },

    // Friendly display name for a credential: the operator-supplied label when
    // present, otherwise the generated email it falls back to.
    credentialName(user) {
      const label = typeof user?.label === "string" ? user.label.trim() : "";
      return label || user?.email || "credential";
    },

    // Display label for the MCP client a credential was minted for, resolved from
    // the stored `client` key. Returns "" for an unknown/absent value so the
    // template can hide the badge entirely.
    credentialClientLabel(user) {
      const key = typeof user?.client === "string" ? user.client : "";
      const match = this.connectClients.find((c) => c.key === key);
      return match ? match.label : "";
    },

    parseScopes(raw) {
      if (!raw || !raw.trim()) return [];
      return raw
        .split(",")
        .map((scope) => scope.trim())
        .filter((scope) => scope.length > 0);
    },

    resetUserForm() {
      this.forms.user = {
        email: "",
        password: "",
        role: "tenant-admin",
        scopes: "",
        status: "active",
      };
    },

    async loadUsers() {
      this.clearError();
      try {
        const payload = await this.apiRequest("/admin/users");
        this.state.users = payload.items || [];
      } catch (error) {
        this.setError(error);
      }
    },

    async createUser() {
      this.clearError();
      this.state.userNotice = "";
      const email = this.forms.user.email;
      try {
        await this.apiRequest("/admin/users", {
          method: "POST",
          body: {
            tenant_id: this.state.tenantId,
            email: this.forms.user.email,
            password: this.forms.user.password,
            roles: this.rolesForSelection(this.forms.user.role),
            scopes: this.parseScopes(this.forms.user.scopes),
            status: this.forms.user.status,
          },
        });
        this.state.userNotice = `Created ${email}.`;
        this.resetUserForm();
        await this.loadUsers();
        this.notify(`User '${email}' created.`);
      } catch (error) {
        this.setError(error);
      }
    },

    async toggleUserStatus(user) {
      this.clearError();
      try {
        const nextStatus = user.status === "active" ? "disabled" : "active";
        await this.apiRequest(`/admin/users/${encodeURIComponent(user.id)}`, {
          method: "PATCH",
          body: { status: nextStatus },
        });
        await this.loadUsers();
        this.notify(
          `User '${user.email}' ${nextStatus === "active" ? "enabled" : "disabled"}.`,
        );
      } catch (error) {
        this.setError(error);
      }
    },

    async resetUserPassword(user) {
      const next = window.prompt(`Set a new password for ${user.email}:`);
      if (!next) return;
      this.clearError();
      try {
        await this.apiRequest(`/admin/users/${encodeURIComponent(user.id)}`, {
          method: "PATCH",
          body: { password: next },
        });
        this.state.userNotice = `Password reset for ${user.email}.`;
        this.notify(`Password reset for '${user.email}'.`);
      } catch (error) {
        this.setError(error);
      }
    },

    async deleteUser(user) {
      if (!window.confirm(`Delete user '${user.email}'?`)) return;
      this.clearError();
      try {
        await this.apiRequest(`/admin/users/${encodeURIComponent(user.id)}`, {
          method: "DELETE",
        });
        await this.loadUsers();
        this.notify(`User '${user.email}' deleted.`, "warning");
      } catch (error) {
        this.setError(error);
      }
    },

    async changeMyPassword() {
      this.clearError();
      this.state.userNotice = "";
      try {
        await this.apiRequest("/admin/users/me/password", {
          method: "POST",
          includeTenant: false,
          body: {
            current_password: this.forms.passwordChange.current_password,
            new_password: this.forms.passwordChange.new_password,
          },
        });
        this.forms.passwordChange = { current_password: "", new_password: "" };
        this.state.userNotice = "Your password has been updated.";
        this.notify("Your password has been updated.");
      } catch (error) {
        this.setError(error);
      }
    },

    async loadPendingActions() {
      this.clearError();
      try {
        const payload = await this.apiRequest("/admin/actions");
        this.state.pendingActions = payload.items || [];
      } catch (error) {
        this.setError(error);
      }
    },

    async approvePendingAction(action) {
      if (!window.confirm(`Approve '${action.server}/${action.tool}'?`)) return;
      this.clearError();
      try {
        await this.apiRequest(
          `/admin/actions/${encodeURIComponent(action.action_id)}/approve`,
          {
            method: "POST",
          },
        );
        await this.loadPendingActions();
        this.notify(`Approved '${action.server}/${action.tool}'.`);
      } catch (error) {
        this.setError(error);
      }
    },

    async rejectPendingAction(action) {
      if (!window.confirm(`Reject '${action.server}/${action.tool}'?`)) return;
      this.clearError();
      try {
        await this.apiRequest(
          `/admin/actions/${encodeURIComponent(action.action_id)}/reject`,
          {
            method: "POST",
          },
        );
        await this.loadPendingActions();
        this.notify(`Rejected '${action.server}/${action.tool}'.`, "warning");
      } catch (error) {
        this.setError(error);
      }
    },

    async loadServers() {
      this.clearError();
      try {
        const payload = await this.apiRequest("/admin/servers");
        this.state.servers = payload.items || [];
        if (this.state.servers.length === 0 && !this.state.serverComposerOpen) {
          this.state.serverComposerOpen = true;
          this.state.serverComposerMode = "create";
          this.$nextTick(() => this.refreshCodeEditors());
        }
      } catch (error) {
        this.setError(error);
      }
    },

    selectedWorkspaceServer() {
      const selectedName = String(this.state.serverWorkspace.server || "").trim();
      if (!selectedName) return null;
      return (
        this.state.servers.find(
          (item) => String(item.server || "").trim() === selectedName,
        ) || null
      );
    },

    workspaceServerName() {
      return (
        String(this.state.serverWorkspace.server || "").trim() ||
        String(this.forms.server.server || "").trim()
      );
    },

    _filenameFromDisposition(header, fallback) {
      if (!header) return fallback;
      const match = /filename="?([^";]+)"?/i.exec(header);
      return match ? match[1].trim() : fallback;
    },

    async exportServer() {
      // Download a self-contained, runnable FastMCP project (.zip) for the
      // current code server. Uses a blob fetch (not apiRequest) since the
      // response is binary; same-origin cookies carry auth, no CSRF needed (GET).
      const server = this.selectedWorkspaceServer();
      const name = String(server?.server || this.workspaceServerName() || "").trim();
      if (!name) {
        this.notify("Open a code server to export it.", "warning");
        return;
      }
      if (this.state.exportingServer) return;
      this.state.exportingServer = true;
      try {
        const url = new URL(
          `/admin/servers/${encodeURIComponent(name)}/export`,
          window.location.origin,
        );
        if (this.state.tenantId) url.searchParams.set("tenant_id", this.state.tenantId);
        const response = await fetch(url.toString(), {
          method: "GET",
          headers: { Accept: "application/zip" },
          credentials: "same-origin",
        });
        if (response.status === 401) {
          window.location.href = `${this.uiPath}/login`;
          return;
        }
        if (!response.ok) {
          let detail = `${response.status} ${response.statusText}`;
          try {
            const payload = await response.json();
            if (payload.detail) detail = payload.detail;
          } catch (_) {
            // non-JSON error body; keep the status text
          }
          throw new Error(detail);
        }
        const blob = await response.blob();
        const filename = this._filenameFromDisposition(
          response.headers.get("Content-Disposition"),
          `${name}-mcp.zip`,
        );
        const toolCount = response.headers.get("X-Export-Tool-Count");
        const objectUrl = window.URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = objectUrl;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        window.URL.revokeObjectURL(objectUrl);
        const suffix =
          toolCount && !Number.isNaN(Number(toolCount))
            ? ` (${toolCount} tool${toolCount === "1" ? "" : "s"} wired up)`
            : "";
        this.notify(`Exported ${name}${suffix}.`, "success");
      } catch (error) {
        this.notify(`Export failed: ${error.message || error}`, "warning");
      } finally {
        this.state.exportingServer = false;
      }
    },

    async openServerWorkspace(server, tab = "tools") {
      if (!server || !String(server.server || "").trim()) return;
      this.activeSection = "servers";
      this.state.serverWorkspace = {
        open: true,
        server: String(server.server || "").trim(),
        tab: tab || "tools",
      };
      await this.editServer(server);
      await this.loadServerEnv();
      this.resetWorkspaceExplore();
    },

    closeServerWorkspace() {
      this.state.serverWorkspace = { open: false, server: "", tab: "tools" };
      this.resetServerForm();
      this.state.serverComposerOpen = false;
      this.state.serverComposerMode = "create";
      this.state.serverEnv = { keys: [], updated_at: null, updated_by: null };
      this.forms.serverEnv = { key: "", value: "" };
      this.state.serverEnvLoading = false;
      this.state.serverEnvSaving = false;
      this.state.workspaceExplore = null;
      this.state.workspaceExploreTargetId = null;
      this.state.searchResults = [];
    },

    setWorkspaceTab(tab) {
      if (!this.state.serverWorkspace.open) return;
      this.state.serverWorkspace = {
        ...this.state.serverWorkspace,
        tab,
      };
      if (tab === "exploreDb") {
        this.ensureWorkspaceExplore();
      }
      if (tab === "search") {
        this.state.searchResults = [];
      }
      if (tab === "secrets") {
        this.loadServerEnv();
      }
    },

    workspaceTabIs(tab) {
      return (
        this.state.serverWorkspace.open &&
        String(this.state.serverWorkspace.tab || "") === String(tab || "")
      );
    },

    openServerComposer(kind = "code") {
      this.resetServerForm();
      this.state.serverComposerOpen = true;
      this.state.serverComposerMode = "create";
      this.state.serverWorkspace = {
        open: true,
        server: "",
        tab: "tools",
      };
      if (kind === "connect") {
        this.chooseServerKind("connect");
      } else {
        this.chooseServerKind("code");
      }
      this.$nextTick(() => this._focusServerComposer());
    },

    closeServerComposer() {
      this.state.serverComposerOpen = false;
      this.state.serverComposerMode = "create";
      this.resetServerForm();
    },

    _nextToolId() {
      this._toolSeq += 1;
      return this._toolSeq;
    },

    emptyToolForm() {
      return {
        local_id: this._nextToolId(),
        name: "",
        description: "",
        action_type: "read",
        requires_confirmation: false,
        always_included: false,
        requirements: "",
        raw_code: "",
        scopes: "",
        input_schema: "{}",
        test_arguments: "{}",
        expanded: true,
        explore_open: false,
        tools_open: false,
        explore_collection: "",
        explore_mode: "find",
        explore_filter: "{}",
        explore_pipeline: "[]",
        explore_limit: 20,
        explore_snippet: "",
        explore_sample_docs: [],
        explore_field_types: {},
        explore_results: [],
        explore_error: "",
        explore_loading: false,
      };
    },

    normalizeToolForm(tool = {}) {
      const meta = tool.metadata || {};
      const requirements = Array.isArray(tool.requirements)
        ? tool.requirements.join("\n")
        : "";
      const scopes = Array.isArray(tool.scopes) ? tool.scopes.join(", ") : "";
      return {
        local_id:
          typeof tool.local_id === "number"
            ? tool.local_id
            : this._nextToolId(),
        name: tool.name || "",
        description: tool.description || "",
        action_type: meta.action_type || "read",
        requires_confirmation: Boolean(meta.requires_confirmation),
        always_included: Boolean(meta.always_included),
        requirements,
        raw_code: tool.raw_code || "",
        scopes,
        input_schema: JSON.stringify(tool.input_schema || {}, null, 2),
        test_arguments: "{}",
        expanded: false,
        explore_open: false,
        tools_open: false,
        explore_collection: "",
        explore_mode: "find",
        explore_filter: "{}",
        explore_pipeline: "[]",
        explore_limit: 20,
        explore_snippet: "",
        explore_sample_docs: [],
        explore_field_types: {},
        explore_results: [],
        explore_error: "",
        explore_loading: false,
      };
    },

    resetServerForm() {
      this.forms.server = {
        server: "",
        transport: "code",
        endpoint: "",
        command: "",
        metadata: '{"domain":"custom","runtime":"wasm"}',
        tools: [this.emptyToolForm()],
      };
      this.state.toolTestResults = {};
      this.state.toolValidation = {};
      this.resetWorkspaceExplore();
      this._teardownCodeEditors();
      this.$nextTick(() => this.refreshCodeEditors());
    },

    resetWorkspaceExplore() {
      this.state.workspaceExplore = {
        explore_collection: "",
        explore_mode: "find",
        explore_filter: "{}",
        explore_pipeline: "[]",
        explore_limit: 20,
        explore_snippet: "",
        explore_sample_docs: [],
        explore_results: [],
        explore_error: "",
        explore_loading: false,
      };
      const firstTool = (this.forms.server.tools || [])[0];
      this.state.workspaceExploreTargetId = firstTool ? firstTool.local_id : null;
    },

    ensureWorkspaceExplore() {
      if (!this.state.workspaceExplore) {
        this.resetWorkspaceExplore();
      }
      this.ensureExploreCollections(false)
        .then(() => {
          if (
            !this.state.workspaceExplore.explore_collection &&
            this.state.exploreCollections.length > 0
          ) {
            this.state.workspaceExplore.explore_collection = this.state.exploreCollections[0];
          }
        })
        .catch((error) => {
          this.state.workspaceExplore.explore_error =
            error instanceof Error ? error.message : String(error || "Failed to load collections.");
        });
    },

    async loadWorkspaceExploreSample() {
      if (!this.state.workspaceExplore) this.resetWorkspaceExplore();
      await this.loadExploreSample(this.state.workspaceExplore);
    },

    async runWorkspaceExploreQuery() {
      if (!this.state.workspaceExplore) this.resetWorkspaceExplore();
      await this.runExploreQuery(this.state.workspaceExplore);
    },

    async copyWorkspaceExploreSnippet() {
      if (!this.state.workspaceExplore) return;
      await this.copyExploreSnippet(this.state.workspaceExplore);
    },

    insertWorkspaceExploreSnippet() {
      if (!this.state.workspaceExplore) return;
      const targetId = this.state.workspaceExploreTargetId;
      const target = (this.forms.server.tools || []).find(
        (tool) => String(tool.local_id) === String(targetId),
      );
      if (!target) {
        this.notify("Choose a target function before inserting.", "warning");
        return;
      }
      target.expanded = true;
      target.explore_snippet = String(this.state.workspaceExplore.explore_snippet || "");
      this.insertExploreSnippet(target);
      this.$nextTick(() => this.refreshCodeEditors());
    },

    async loadServerEnv() {
      const serverName = String(this.state.serverWorkspace.server || "").trim();
      if (!serverName) return;
      this.state.serverEnvLoading = true;
      try {
        const payload = await this.apiRequest(
          `/admin/servers/${encodeURIComponent(serverName)}/env`,
        );
        this.state.serverEnv = {
          keys: Array.isArray(payload.keys) ? payload.keys : [],
          updated_at: payload.updated_at || null,
          updated_by: payload.updated_by || null,
        };
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.serverEnvLoading = false;
      }
    },

    async saveServerEnvEntry() {
      const serverName = String(this.state.serverWorkspace.server || "").trim();
      const key = String(this.forms.serverEnv.key || "").trim();
      const value = String(this.forms.serverEnv.value || "");
      if (!serverName) {
        this.setError("Select a server first.");
        return;
      }
      if (!key) {
        this.setError("Environment key is required.");
        return;
      }
      this.state.serverEnvSaving = true;
      try {
        const payload = await this.apiRequest(
          `/admin/servers/${encodeURIComponent(serverName)}/env`,
          {
            method: "PUT",
            body: { values: { [key]: value } },
          },
        );
        this.state.serverEnv = {
          keys: Array.isArray(payload.keys) ? payload.keys : [],
          updated_at: payload.updated_at || null,
          updated_by: payload.updated_by || null,
        };
        this.forms.serverEnv.value = "";
        this.notify(`Saved env key '${key}'.`, "success");
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.serverEnvSaving = false;
      }
    },

    editServerEnvKey(key) {
      this.forms.serverEnv.key = String(key || "");
      this.forms.serverEnv.value = "";
    },

    async deleteServerEnvKey(key) {
      const normalized = String(key || "").trim();
      if (!normalized) return;
      const serverName = String(this.state.serverWorkspace.server || "").trim();
      if (!serverName) return;
      this.state.serverEnvSaving = true;
      try {
        const payload = await this.apiRequest(
          `/admin/servers/${encodeURIComponent(serverName)}/env`,
          {
            method: "PUT",
            body: { values: { [normalized]: "" } },
          },
        );
        this.state.serverEnv = {
          keys: Array.isArray(payload.keys) ? payload.keys : [],
          updated_at: payload.updated_at || null,
          updated_by: payload.updated_by || null,
        };
        if (this.forms.serverEnv.key === normalized) {
          this.forms.serverEnv.key = "";
          this.forms.serverEnv.value = "";
        }
        this.notify(`Deleted env key '${normalized}'.`, "warning");
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.serverEnvSaving = false;
      }
    },

    addToolToServer() {
      const tool = this.emptyToolForm();
      this.forms.server.tools.push(tool);
      this._ensureSingleActiveTool(tool.local_id);
      this.$nextTick(() => this.refreshCodeEditors());
    },

    // A "virtual" server is authored code; "connect" proxies an existing MCP
    // server over a network/stdio transport. The chooser maps to the stored
    // transport field while keeping the rest of the form intact.
    chooseServerKind(kind) {
      if (kind === "code") {
        this.forms.server.transport = "code";
      } else if (this.forms.server.transport === "code") {
        this.forms.server.transport = "streamable_http";
      }
      this.$nextTick(() => this.refreshCodeEditors());
    },

    serverKindIsCode() {
      return this.forms.server.transport === "code";
    },

    // Master/detail: exactly one function is "active" (its editor is shown).
    _ensureSingleActiveTool(preferId = null) {
      const tools = this.forms.server.tools || [];
      if (tools.length === 0) return null;
      const active =
        tools.find((t) => t.local_id === preferId) ||
        tools.find((t) => t.expanded) ||
        tools[0];
      for (const t of tools) t.expanded = t === active;
      this.state.workspaceExploreTargetId = active.local_id;
      return active;
    },

    selectTool(localId) {
      this._ensureSingleActiveTool(localId);
      this.$nextTick(() => this.refreshCodeEditors());
    },

    // Back-compat shims (older callers); both now select a single function.
    toggleTool(localId) {
      this.selectTool(localId);
    },

    expandTool(localId) {
      this.selectTool(localId);
    },

    toolSummaryName(tool, idx) {
      const name = String(tool?.name || "").trim();
      return name || `Function ${idx + 1}`;
    },

    _setToolSource(tool, nextSource) {
      const next = String(nextSource || "");
      tool.raw_code = next;
      const view = this._codeEditors[String(tool.local_id)];
      if (view) {
        const current = view.state.doc.toString();
        if (current !== next) {
          view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: next } });
        }
      }
      this.scheduleToolValidate(tool);
    },

    async ensureExploreCollections(force = false) {
      const tenantId = String(this.state.tenantId || "");
      if (!tenantId) return;
      if (
        !force &&
        this.state.exploreCollectionsTenant === tenantId &&
        (this.state.exploreCollections || []).length > 0
      ) {
        return;
      }
      this.state.exploreCollectionsLoading = true;
      try {
        const payload = await this.apiRequest("/admin/explore/collections", {
          includeTenant: false,
        });
        this.state.exploreCollections = Array.isArray(payload.collections)
          ? payload.collections
          : [];
        this.state.exploreCollectionsTenant = tenantId;
      } finally {
        this.state.exploreCollectionsLoading = false;
      }
    },

    async toggleExplore(tool) {
      if (!tool) return;
      tool.explore_open = !tool.explore_open;
      tool.explore_error = "";
      if (!tool.explore_open) return;
      try {
        await this.ensureExploreCollections(false);
        if (!tool.explore_collection && this.state.exploreCollections.length > 0) {
          tool.explore_collection = this.state.exploreCollections[0];
        }
      } catch (error) {
        tool.explore_error =
          error instanceof Error ? error.message : String(error || "Failed to load collections.");
      }
    },

    async loadExploreSample(tool) {
      if (!tool || !String(tool.explore_collection || "").trim()) {
        tool.explore_error = "Choose a collection first.";
        return;
      }
      tool.explore_error = "";
      tool.explore_loading = true;
      try {
        const payload = await this.apiRequest("/admin/explore/sample", {
          method: "POST",
          includeTenant: false,
          body: {
            tenant_id: this.state.tenantId,
            collection: String(tool.explore_collection || "").trim(),
            limit: Number(tool.explore_limit || 20),
          },
        });
        tool.explore_field_types = payload.field_types || {};
        tool.explore_sample_docs = payload.sample_docs || [];
        tool.explore_snippet = String(payload.snippet || "");
        tool.explore_results = [];
        if (!String(tool.explore_filter || "").trim()) {
          tool.explore_filter = "{}";
        }
      } catch (error) {
        tool.explore_error =
          error instanceof Error ? error.message : String(error || "Failed to sample collection.");
      } finally {
        tool.explore_loading = false;
      }
    },

    async runExploreQuery(tool) {
      if (!tool || !String(tool.explore_collection || "").trim()) {
        tool.explore_error = "Choose a collection first.";
        return;
      }
      tool.explore_error = "";
      tool.explore_loading = true;
      try {
        const mode = tool.explore_mode === "aggregate" ? "aggregate" : "find";
        const body = {
          tenant_id: this.state.tenantId,
          collection: String(tool.explore_collection || "").trim(),
          mode,
          limit: Number(tool.explore_limit || 20),
        };
        if (mode === "aggregate") {
          body.pipeline = this.parseJsonArray(tool.explore_pipeline, "Aggregate pipeline");
        } else {
          body.filter = this.parseJsonObject(tool.explore_filter, "Find filter");
        }
        const payload = await this.apiRequest("/admin/explore/query", {
          method: "POST",
          includeTenant: false,
          body,
        });
        tool.explore_results = payload.results || [];
        tool.explore_snippet = String(payload.snippet || "");
      } catch (error) {
        tool.explore_error =
          error instanceof Error ? error.message : String(error || "Explore query failed.");
      } finally {
        tool.explore_loading = false;
      }
    },

    async copyExploreSnippet(tool) {
      const snippet = String(tool?.explore_snippet || "").trim();
      if (!snippet) return;
      try {
        await navigator.clipboard.writeText(snippet);
        this.notify("Copied query snippet.", "success");
      } catch (_) {
        this.notify("Clipboard permission denied. Copy manually.", "warning");
      }
    },

    insertExploreSnippet(tool) {
      const snippet = String(tool?.explore_snippet || "").trim();
      if (!snippet) return;
      const current = String(tool.raw_code || "").trimEnd();
      const next = current ? `${current}\n\n${snippet}\n` : `${snippet}\n`;
      this._setToolSource(tool, next);
      tool.expanded = true;
      this.notify("Inserted query snippet into function source.", "success");
    },

    // ---- context.tools palette (call sibling tools) -----------------------

    toggleToolPalette(tool) {
      if (!tool) return;
      tool.tools_open = !tool.tools_open;
      if (tool.tools_open) tool.explore_open = false;
    },

    _toolParams(entry) {
      // Pull parameter names/types from the tool's JSON input schema so authors
      // see exactly what to pass — required first, optional after.
      const schema =
        entry && typeof entry.input_schema === "object" ? entry.input_schema : {};
      const props =
        schema && typeof schema.properties === "object" ? schema.properties : {};
      const required = new Set(
        Array.isArray(schema.required) ? schema.required.map(String) : [],
      );
      const params = Object.keys(props).map((name) => ({
        name,
        type: String((props[name] || {}).type || ""),
        required: required.has(name),
      }));
      params.sort((a, b) => Number(b.required) - Number(a.required));
      return params;
    },

    callableToolGroups(currentTool) {
      // Build the palette client-side from the tenant's servers: only code
      // servers expose callable tools, mirroring the host's code-only policy.
      const filter = String(this.state.toolPaletteFilter || "")
        .trim()
        .toLowerCase();
      const currentName = String(currentTool?.name || "").trim();
      const currentServer = String(this.workspaceServerName() || "").trim();
      const groups = [];
      for (const server of this.state.servers || []) {
        if (String(server.transport || "") !== "code") continue;
        const serverName = String(server.server || "").trim();
        if (!serverName) continue;
        const tools = [];
        for (const t of server.tools || []) {
          const name = String(t.name || "").trim();
          if (!name) continue;
          // Don't suggest a tool calling itself (the obvious infinite loop).
          if (serverName === currentServer && name === currentName) continue;
          const params = this._toolParams(t);
          const entry = {
            server: serverName,
            name,
            description: String(t.description || "").trim(),
            params,
            paramLabel: params.map((p) => p.name).join(", "),
            input_schema: t.input_schema,
          };
          const haystack = `${serverName} ${name} ${entry.description}`.toLowerCase();
          if (filter && !haystack.includes(filter)) continue;
          tools.push(entry);
        }
        if (tools.length > 0) {
          tools.sort((a, b) => a.name.localeCompare(b.name));
          groups.push({ server: serverName, tools });
        }
      }
      groups.sort((a, b) => a.server.localeCompare(b.server));
      return groups;
    },

    _toolCallSnippet(entry) {
      // Bracket form works for any server/tool name (incl. hyphens). Required
      // params get a TODO sentinel; optional ones are omitted to keep it tidy.
      const required = (entry.params || []).filter((p) => p.required);
      const args = required.map((p) => `${p.name}=...`).join(", ");
      return `result = context.tools[${JSON.stringify(
        entry.server,
      )}][${JSON.stringify(entry.name)}](${args})`;
    },

    async copyToolCall(entry) {
      const snippet = this._toolCallSnippet(entry);
      try {
        await navigator.clipboard.writeText(snippet);
        this.notify("Copied tool call.", "success");
      } catch (_) {
        this.notify("Clipboard permission denied. Copy manually.", "warning");
      }
    },

    _insertIntoToolSource(tool, text) {
      // Prefer inserting at the cursor in the live editor; fall back to append.
      const view = this._codeEditors[String(tool.local_id)];
      if (view) {
        const insert = `${text}\n`;
        view.dispatch(view.state.replaceSelection(insert));
        tool.raw_code = view.state.doc.toString();
        view.focus();
        this.scheduleToolValidate(tool);
        return;
      }
      const current = String(tool.raw_code || "").trimEnd();
      this._setToolSource(tool, current ? `${current}\n${text}\n` : `${text}\n`);
    },

    insertToolCall(tool, entry) {
      if (!tool || !entry) return;
      this._insertIntoToolSource(tool, this._toolCallSnippet(entry));
      tool.expanded = true;
      this.notify(`Inserted call to ${entry.server}.${entry.name}.`, "success");
    },

    // One-click "see the magic": drop in a complete, sandbox-safe function the
    // operator can immediately run in the sandbox and save as a real MCP tool.
    loadExampleTool() {
      const example = this.emptyToolForm();
      example.name = "word_count";
      example.description = "Count the words and characters in a string.";
      example.action_type = "read";
      example.scopes = "utilities, readonly";
      example.input_schema = JSON.stringify(
        {
          type: "object",
          properties: {
            text: { type: "string", description: "Text to analyze" },
          },
          required: ["text"],
        },
        null,
        2,
      );
      example.raw_code = [
        "def word_count(text: str) -> dict:",
        '    """Count the words and characters in the given text."""',
        "    words = [w for w in text.split() if w]",
        '    return {"words": len(words), "characters": len(text)}',
        "",
      ].join("\n");
      example.test_arguments = JSON.stringify({
        text: "hello brave new world",
      });
      example.expanded = true;

      const tools = this.forms.server.tools || [];
      const onlyEmptyStarter =
        tools.length === 1 &&
        !String(tools[0].name || "").trim() &&
        !String(tools[0].raw_code || "").trim();
      if (onlyEmptyStarter) {
        this._teardownCodeEditor(tools[0].local_id);
        this.forms.server.tools = [example];
      } else {
        this.forms.server.tools.push(example);
      }
      this._ensureSingleActiveTool(example.local_id);
      if (!String(this.forms.server.server || "").trim()) {
        this.forms.server.server = "my-tools";
      }
      this.forms.server.transport = "code";
      this.$nextTick(() => this.refreshCodeEditors());
    },

    removeToolFromServer(localId) {
      if ((this.forms.server.tools || []).length <= 1) {
        this.setError("A code server must include at least one function.");
        return;
      }
      const removedActive = Boolean(this._toolById(localId)?.expanded);
      this.forms.server.tools = (this.forms.server.tools || []).filter(
        (tool) => tool.local_id !== localId,
      );
      this._teardownCodeEditor(localId);
      // Removing the visible function falls back to the first remaining one.
      this._ensureSingleActiveTool(
        removedActive ? null : this.state.workspaceExploreTargetId,
      );
      this.$nextTick(() => this.refreshCodeEditors());
    },

    async editServer(server) {
      this.state.serverComposerOpen = true;
      this.state.serverComposerMode = "edit";
      this.state.serverWorkspace = {
        open: true,
        server: String(server.server || "").trim(),
        tab: this.state.serverWorkspace?.tab || "tools",
      };
      this.forms.server = {
        server: server.server || "",
        transport: server.transport || "streamable_http",
        endpoint: server.endpoint || "",
        command: server.command || "",
        metadata: JSON.stringify(server.metadata || {}),
        tools: [this.emptyToolForm()],
      };
      this._teardownCodeEditors();
      if (server.transport !== "code") return;
      // The list view redacts authored source; fetch the single server to load
      // the decrypted functions back into the editor.
      this.clearError();
      try {
        const detail = await this.apiRequest(
          `/admin/servers/${encodeURIComponent(server.server)}`,
        );
        const tools = (detail.tools || []).map((tool) =>
          this.normalizeToolForm(tool),
        );
        if (tools.length > 0) {
          // Select the first function so its editor is visible right away.
          this.forms.server.tools = tools;
          this._ensureSingleActiveTool(tools[0].local_id);
        } else {
          this.forms.server.tools = [this.emptyToolForm()];
          this._ensureSingleActiveTool(this.forms.server.tools[0].local_id);
        }
        this.state.toolTestResults = {};
        this.state.toolValidation = {};
        this.resetWorkspaceExplore();
        this.$nextTick(() => this.refreshCodeEditors());
        // Surface the contract status for loaded functions right away.
        for (const tool of this.forms.server.tools) this.scheduleToolValidate(tool);
      } catch (error) {
        this.setError(error);
      }
    },

    async startServerEdit(server) {
      await this.openServerWorkspace(server, "tools");
      this.$nextTick(() => this._focusServerComposer());
    },

    _focusServerComposer() {
      const form = document.querySelector(".server-composer");
      if (form) {
        form.scrollIntoView({ behavior: "smooth", block: "start" });
      }
      const input = document.querySelector('[x-model="forms.server.server"]');
      if (input && typeof input.focus === "function") {
        input.focus();
      }
    },

    parseMetadata(raw) {
      return this.parseJsonObject(raw, "Metadata");
    },

    parseJsonObject(raw, fieldName) {
      const source = String(raw || "").trim();
      if (!source) return {};
      const parsed = JSON.parse(source);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error(`${fieldName} must be a JSON object.`);
      }
      return parsed;
    },

    parseJsonArray(raw, fieldName) {
      const source = String(raw || "").trim();
      if (!source) return [];
      const parsed = JSON.parse(source);
      if (!Array.isArray(parsed)) {
        throw new Error(`${fieldName} must be a JSON array.`);
      }
      return parsed;
    },

    async _loadCodeMirror() {
      if (this._cmLib) return this._cmLib;
      if (this._cmLoadPromise) return this._cmLoadPromise;
      this._cmLoadPromise = (async () => {
        // Bare specifiers resolve through the import map in base.html, which pins a
        // single @codemirror/state instance across every package (see comment there).
        const [stateMod, viewMod, commandsMod, languageMod, pythonMod, highlightMod] =
          await Promise.all([
            import("@codemirror/state"),
            import("@codemirror/view"),
            import("@codemirror/commands"),
            import("@codemirror/language"),
            import("@codemirror/lang-python"),
            import("@lezer/highlight"),
          ]);
        this._cmLib = {
          EditorState: stateMod.EditorState,
          EditorView: viewMod.EditorView,
          keymap: viewMod.keymap,
          lineNumbers: viewMod.lineNumbers,
          highlightActiveLineGutter: viewMod.highlightActiveLineGutter,
          highlightSpecialChars: viewMod.highlightSpecialChars,
          drawSelection: viewMod.drawSelection,
          dropCursor: viewMod.dropCursor,
          rectangularSelection: viewMod.rectangularSelection,
          highlightActiveLine: viewMod.highlightActiveLine,
          defaultKeymap: commandsMod.defaultKeymap,
          history: commandsMod.history,
          historyKeymap: commandsMod.historyKeymap,
          indentWithTab: commandsMod.indentWithTab,
          indentOnInput: languageMod.indentOnInput,
          syntaxHighlighting: languageMod.syntaxHighlighting,
          defaultHighlightStyle: languageMod.defaultHighlightStyle,
          // classHighlighter tags tokens with stable `tok-*` classes so the palette
          // lives in styles.css and follows the active theme (see .code-editor-host).
          classHighlighter: highlightMod.classHighlighter,
          bracketMatching: languageMod.bracketMatching,
          foldGutter: languageMod.foldGutter,
          foldKeymap: languageMod.foldKeymap,
          python: pythonMod.python,
        };
        return this._cmLib;
      })().catch((error) => {
        this._cmLoadPromise = null;
        throw error;
      });
      return this._cmLoadPromise;
    },

    _teardownCodeEditor(toolId) {
      const editor = this._codeEditors[toolId];
      if (!editor) return;
      try {
        editor.destroy();
      } catch (_) {
        // Ignore editor teardown errors; DOM removal still clears state.
      }
      delete this._codeEditors[toolId];
    },

    _teardownCodeEditors() {
      for (const key of Object.keys(this._codeEditors)) {
        this._teardownCodeEditor(key);
      }
    },

    _toolById(toolId) {
      return (this.forms.server.tools || []).find(
        (item) => String(item.local_id) === String(toolId),
      );
    },

    async refreshCodeEditors() {
      if (this.forms.server.transport !== "code") {
        this._teardownCodeEditors();
        return;
      }
      let cm;
      try {
        cm = await this._loadCodeMirror();
      } catch (_) {
        // CDN/module load failed; reveal the plain textareas and hide the empty
        // editor hosts so authored code is always visible and editable.
        this._revealFallbacks();
        return;
      }
      // Only expanded functions own a live editor — collapsed/removed cards are
      // torn down so hidden hosts never mis-measure and the DOM stays light.
      const expandedIds = new Set(
        (this.forms.server.tools || [])
          .filter((tool) => tool.expanded)
          .map((tool) => String(tool.local_id)),
      );
      for (const editorId of Object.keys(this._codeEditors)) {
        if (!expandedIds.has(String(editorId))) {
          this._teardownCodeEditor(editorId);
        }
      }
      for (const tool of this.forms.server.tools || []) {
        if (!tool.expanded) continue;
        const toolId = String(tool.local_id);
        if (this._codeEditors[toolId]) continue;
        const host = document.querySelector(
          `[data-code-editor-host="${toolId}"]`,
        );
        const fallback = document.querySelector(
          `[data-code-editor-fallback="${toolId}"]`,
        );
        if (!host || !fallback) continue;
        const startCode = String(tool.raw_code || "");
        const saveBinding = cm.keymap.of([
          {
            key: "Mod-s",
            run: () => {
              this.saveServer();
              return true;
            },
          },
        ]);
        let view;
        try {
          const state = cm.EditorState.create({
            doc: startCode,
            extensions: [
              cm.lineNumbers(),
              cm.highlightActiveLineGutter(),
              cm.highlightSpecialChars(),
              cm.history(),
              cm.drawSelection(),
              cm.dropCursor(),
              cm.indentOnInput(),
              cm.bracketMatching(),
              cm.foldGutter(),
              cm.rectangularSelection(),
              cm.highlightActiveLine(),
              cm.syntaxHighlighting(cm.classHighlighter || cm.defaultHighlightStyle, {
                fallback: true,
              }),
              cm.keymap.of([
                ...cm.defaultKeymap,
                ...cm.historyKeymap,
                ...cm.foldKeymap,
                cm.indentWithTab,
              ]),
              saveBinding,
              cm.python(),
              cm.EditorView.updateListener.of((update) => {
                if (!update.docChanged) return;
                const next = update.state.doc.toString();
                const matched = this._toolById(tool.local_id);
                if (matched) {
                  matched.raw_code = next;
                  // Self-heal: a brand-new function with no name yet inherits the
                  // name you type, so the tool name and function name can't drift.
                  if (!String(matched.name || "").trim()) {
                    const detected = this.detectFunctionName(next);
                    if (detected) matched.name = detected;
                  }
                  this.scheduleToolValidate(matched);
                }
              }),
            ],
          });
          view = new cm.EditorView({ state, parent: host });
        } catch (_) {
          // Editor construction failed — keep the plain textarea usable so the
          // author never loses sight of their code.
          host.style.display = "none";
          fallback.style.display = "";
          fallback.classList.remove("code-editor-fallback");
          continue;
        }
        // Success: swap the textarea out for the rich editor.
        host.style.display = "";
        fallback.classList.add("code-editor-fallback");
        fallback.style.display = "none";
        this._codeEditors[toolId] = view;
      }
    },

    _revealFallbacks() {
      for (const tool of this.forms.server.tools || []) {
        const id = String(tool.local_id);
        const host = document.querySelector(`[data-code-editor-host="${id}"]`);
        const fallback = document.querySelector(
          `[data-code-editor-fallback="${id}"]`,
        );
        if (host) host.style.display = "none";
        if (fallback) {
          fallback.style.display = "";
          fallback.classList.remove("code-editor-fallback");
        }
      }
    },

    buildCodeTools() {
      const tools = this.forms.server.tools || [];
      return tools.map((tool) => {
        const requirements = (tool.requirements || "")
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean);
        const scopes = (tool.scopes || "")
          .split(",")
          .map((scope) => scope.trim())
          .filter(Boolean);
        return {
          server: this.forms.server.server,
          name: tool.name,
          description: tool.description,
          input_schema: this.parseJsonObject(tool.input_schema, "Input schema"),
          scopes,
          raw_code: tool.raw_code,
          requirements,
          metadata: {
            action_type: tool.action_type,
            requires_confirmation: Boolean(tool.requires_confirmation),
            always_included: Boolean(tool.always_included),
          },
        };
      });
    },

    alwaysIncludedCount() {
      return (this.forms.server.tools || []).filter(
        (tool) => tool.always_included,
      ).length;
    },

    // Instant, regex-based preview lint. Intentionally a subset of the server's
    // AST validator (it can't catch syntax errors) — its job is sub-millisecond
    // feedback while typing and to gate the Save button. The authoritative result
    // comes from validateTool() / the pre-save server check.
    lintHintsForTool(tool) {
      const code = String(tool?.raw_code || "");
      const hints = [];
      const bannedImports =
        /\b(import|from)\s+(os|sys|subprocess|socket|shutil|ctypes|multiprocessing|importlib|marshal|pickle|builtins|resource|signal|pty)\b/;
      if (bannedImports.test(code)) {
        hints.push("Uses an import blocked by sandbox policy (os/sys/subprocess/…).");
      }
      const bannedCalls =
        /\b(eval|exec|compile|__import__|globals|locals|vars|open|breakpoint)\s*\(/;
      if (bannedCalls.test(code)) {
        hints.push("Uses a blocked function call (eval/exec/open/…).");
      }
      const bannedAttrs =
        /\.(__globals__|__builtins__|__subclasses__|__bases__|__mro__|__code__|__dict__|__getattribute__|__reduce__)\b/;
      if (bannedAttrs.test(code)) {
        hints.push("Accesses a blocked dunder attribute (sandbox-escape pattern).");
      }
      if (new Blob([code]).size > 64 * 1024) {
        hints.push("Source exceeds the 64KB sandbox limit.");
      }
      const name = String(tool?.name || "").trim();
      if (
        name &&
        code.trim() &&
        !new RegExp(`(\\bdef\\s+${name}\\s*\\()|(^\\s*${name}\\s*=)`, "m").test(code)
      ) {
        hints.push(`Define a function named '${name}' — it must match the tool name.`);
      }
      for (const req of this._requirementLines(tool)) {
        if (!/^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?:\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9][A-Za-z0-9.\-_+!]*$/.test(req)) {
          hints.push(`Requirement '${req}' must be pinned like 'package==1.2.3'.`);
        }
      }
      const schemaRaw = String(tool?.input_schema ?? "").trim();
      if (schemaRaw) {
        try {
          const parsed = JSON.parse(schemaRaw);
          if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
            hints.push("Input schema must be a JSON object.");
          }
        } catch (_) {
          hints.push("Input schema is not valid JSON.");
        }
      }
      return hints;
    },

    _parsedInputSchema(tool) {
      const raw = String(tool?.input_schema ?? "").trim();
      if (!raw) return {};
      try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          return parsed;
        }
      } catch (_) {
        // Invalid JSON is flagged separately by lintHintsForTool.
      }
      return {};
    },

    _requirementLines(tool) {
      return String(tool?.requirements || "")
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
    },

    hasLintHints(tool) {
      return this.lintHintsForTool(tool).length > 0;
    },

    // Best-effort detection of the callable a tool defines: the first top-level
    // `def NAME(` (or `async def`), else a top-level `NAME =` binding. Top-level
    // only — indented defs won't match `^def`.
    detectFunctionName(code) {
      const src = String(code || "");
      const def = src.match(/^(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(/m);
      if (def) return def[1];
      const assign = src.match(/^([A-Za-z_]\w*)\s*=(?!=)/m);
      return assign ? assign[1] : "";
    },

    applyToolNameFix(tool) {
      const detected = this.detectFunctionName(tool.raw_code);
      if (!detected) return;
      tool.name = detected;
      this.notify(`Tool renamed to '${detected}' to match your function.`, "success");
      this.scheduleToolValidate(tool);
    },

    focusToolLine(tool, line) {
      const view = this._codeEditors[String(tool.local_id)];
      if (!view || !line) return;
      try {
        const target = Math.max(1, Math.min(line, view.state.doc.lines));
        const lineInfo = view.state.doc.line(target);
        view.dispatch({
          selection: { anchor: lineInfo.from },
          scrollIntoView: true,
        });
        view.focus();
      } catch (_) {
        // Editor may be collapsed/unmounted; nothing to focus.
      }
    },

    // ---- Authoritative server-side validation (AST lint, no execution) -------
    getToolValidation(toolId) {
      return (this.state.toolValidation || {})[String(toolId)] || null;
    },

    _setToolValidation(toolId, next) {
      this.state.toolValidation = {
        ...(this.state.toolValidation || {}),
        [String(toolId)]: next,
      };
    },

    async validateTool(tool) {
      if (!tool) return { ok: true, issues: [] };
      const toolId = tool.local_id;
      this._setToolValidation(toolId, {
        ...(this.getToolValidation(toolId) || {}),
        validating: true,
      });
      try {
        const response = await this.apiRequest("/admin/code-tools/validate", {
          method: "POST",
          includeTenant: false,
          body: {
            name: String(tool.name || "").trim(),
            raw_code: String(tool.raw_code || ""),
            requirements: this._requirementLines(tool),
            action_type: tool.action_type || "read",
            input_schema: this._parsedInputSchema(tool),
          },
        });
        this._setToolValidation(toolId, {
          validating: false,
          ok: Boolean(response.ok),
          issues: Array.isArray(response.issues) ? response.issues : [],
          suggested_schema: response.suggested_schema || null,
        });
        return response;
      } catch (error) {
        // Network/validate failure must not silently green-light a save: record a
        // soft error but let the pre-save server check be the hard gate.
        this._setToolValidation(toolId, {
          validating: false,
          ok: false,
          issues: [
            {
              severity: "error",
              message:
                error instanceof Error
                  ? `Validation unavailable: ${error.message}`
                  : "Validation unavailable.",
              line: null,
            },
          ],
        });
        return { ok: false, issues: [] };
      }
    },

    scheduleToolValidate(tool) {
      if (!tool) return;
      const toolId = String(tool.local_id);
      if (this._validateTimers[toolId]) {
        window.clearTimeout(this._validateTimers[toolId]);
      }
      // Empty source: clear stale results, nothing to lint yet.
      if (!String(tool.raw_code || "").trim()) {
        this._setToolValidation(toolId, { validating: false, ok: false, issues: [] });
        return;
      }
      this._validateTimers[toolId] = window.setTimeout(() => {
        delete this._validateTimers[toolId];
        const live = this._toolById(tool.local_id);
        if (live) this.validateTool(live);
      }, 450);
    },

    // Merged, de-duplicated issues for display: authoritative server issues when
    // available, otherwise the instant client preview.
    toolIssues(tool) {
      if (!tool) return [];
      const validation = this.getToolValidation(tool.local_id);
      if (validation && Array.isArray(validation.issues) && !validation.validating) {
        return validation.issues;
      }
      return this.lintHintsForTool(tool).map((message) => ({
        severity: "error",
        message,
        line: null,
      }));
    },

    toolErrorCount(tool) {
      return this.toolIssues(tool).filter((i) => i.severity === "error").length;
    },

    toolWarningCount(tool) {
      return this.toolIssues(tool).filter((i) => i.severity === "warning").length;
    },

    // The name-mismatch error carries a one-click fix when we can detect the
    // function the author actually wrote.
    issueFixName(tool, issue) {
      if (!issue || !String(issue.message || "").includes("must match the tool name")) {
        return "";
      }
      return this.detectFunctionName(tool.raw_code);
    },

    toolSuggestedSchema(tool) {
      return this.getToolValidation(tool?.local_id)?.suggested_schema || null;
    },

    toolHasSchemaWarning(tool) {
      return this.toolIssues(tool).some(
        (issue) => issue.severity === "warning" && /schema/i.test(issue.message),
      );
    },

    // Offer the sync affordance whenever the signature implies a schema that
    // differs from what's authored (covers both "empty schema" and "drift").
    canSyncSchema(tool) {
      const suggested = this.toolSuggestedSchema(tool);
      if (!suggested) return false;
      const current = this._parsedInputSchema(tool);
      return JSON.stringify(current) !== JSON.stringify(this._mergeSchema(current, suggested));
    },

    // Merge the signature-derived schema over the current one, preserving any
    // human-authored property descriptions/extras the author already wrote.
    _mergeSchema(current, suggested) {
      const curProps = (current && current.properties) || {};
      const properties = {};
      for (const [key, value] of Object.entries(suggested.properties || {})) {
        properties[key] = { ...(curProps[key] || {}), ...value };
      }
      const merged = { type: "object", properties };
      if (Array.isArray(suggested.required) && suggested.required.length) {
        merged.required = suggested.required;
      }
      return merged;
    },

    applySuggestedSchema(tool) {
      const suggested = this.toolSuggestedSchema(tool);
      if (!suggested) return;
      const merged = this._mergeSchema(this._parsedInputSchema(tool), suggested);
      tool.input_schema = JSON.stringify(merged, null, 2);
      const count = Object.keys(merged.properties || {}).length;
      this.notify(
        `Input schema synced from signature (${count} field${count === 1 ? "" : "s"}).`,
        "success",
      );
      this.scheduleToolValidate(tool);
    },

    // ---- Test-argument seeding ---------------------------------------------
    _sampleValue(def) {
      const d = def || {};
      if (Array.isArray(d.enum) && d.enum.length) return d.enum[0];
      if (d.default !== undefined) return d.default;
      switch (d.type) {
        case "integer":
        case "number":
          return 1;
        case "boolean":
          return true;
        case "array":
          return [];
        case "object":
          return {};
        case "string":
        default:
          return "example";
      }
    },

    // A representative argument object from the schema (or, if the schema is
    // empty, the signature-derived suggestion) so "Run" has inputs to work with.
    _sampleArgsFor(tool) {
      let schema = this._parsedInputSchema(tool);
      if (!schema || !schema.properties || !Object.keys(schema.properties).length) {
        schema = this.toolSuggestedSchema(tool) || {};
      }
      const props = (schema && schema.properties) || {};
      const out = {};
      for (const [key, def] of Object.entries(props)) {
        out[key] = this._sampleValue(def);
      }
      return out;
    },

    canFillTestArgs(tool) {
      return Object.keys(this._sampleArgsFor(tool)).length > 0;
    },

    fillTestArgsFromSchema(tool) {
      const sample = this._sampleArgsFor(tool);
      const count = Object.keys(sample).length;
      if (!count) {
        this.notify("No input fields to fill — add an input schema first.", "warning");
        return;
      }
      tool.test_arguments = JSON.stringify(sample, null, 2);
      this.notify(`Filled ${count} test argument${count === 1 ? "" : "s"} from schema.`, "success");
    },

    // True when a tool has problems that must block the save: instant client
    // errors, or an authoritative server validation that found errors.
    toolHasBlockingProblem(tool) {
      if (this.toolBlockingHints(tool).length > 0) return true;
      const validation = this.getToolValidation(tool.local_id);
      return Boolean(validation && !validation.validating && validation.ok === false);
    },

    // Presence + instant-lint problems that should block saving. Used to disable
    // the Save button immediately (no round-trip needed for the obvious cases).
    toolBlockingHints(tool) {
      const hints = [];
      if (!String(tool?.name || "").trim()) hints.push("Function needs a name.");
      if (!String(tool?.raw_code || "").trim())
        hints.push("Function needs Python source.");
      for (const hint of this.lintHintsForTool(tool)) hints.push(hint);
      return hints;
    },

    canSaveServer() {
      if (!String(this.forms.server.server || "").trim()) return false;
      if (!this.serverKindIsCode()) {
        return Boolean(
          String(this.forms.server.endpoint || "").trim() ||
            String(this.forms.server.command || "").trim(),
        );
      }
      const tools = this.forms.server.tools || [];
      if (tools.length === 0) return false;
      return tools.every((tool) => !this.toolHasBlockingProblem(tool));
    },

    saveBlockedReason() {
      if (!String(this.forms.server.server || "").trim()) {
        return "Set a server name to save.";
      }
      if (!this.serverKindIsCode()) {
        if (
          !String(this.forms.server.endpoint || "").trim() &&
          !String(this.forms.server.command || "").trim()
        ) {
          return "Set an endpoint or command to save.";
        }
        return "";
      }
      const tools = this.forms.server.tools || [];
      if (tools.length === 0) return "Add at least one function to save.";
      const broken = tools.filter((tool) => this.toolHasBlockingProblem(tool));
      if (broken.length === 0) return "";
      const first = broken[0];
      const label = String(first.name || "").trim() || "Unnamed function";
      const hints = this.toolBlockingHints(first);
      let message = hints[0];
      if (!message) {
        const validation = this.getToolValidation(first.local_id);
        const firstError = (validation?.issues || []).find((i) => i.severity === "error");
        message = firstError ? firstError.message : "resolve validation issues";
      }
      return `Fix '${label}': ${message}`;
    },

    _setToolTestResult(toolId, next) {
      this.state.toolTestResults = {
        ...(this.state.toolTestResults || {}),
        [String(toolId)]: next,
      };
    },

    getToolTestResult(toolId) {
      return (this.state.toolTestResults || {})[String(toolId)] || null;
    },

    async runToolTest(tool) {
      this.clearError();
      const serverName = String(this.forms.server.server || "").trim();
      if (!serverName) {
        this.setError("Set a server name before running a function test.");
        return;
      }
      const toolName = String(tool?.name || "").trim();
      if (!toolName) {
        this.setError("Set a function name before running a test.");
        return;
      }
      let argumentsPayload = {};
      try {
        argumentsPayload = this.parseJsonObject(
          tool.test_arguments,
          "Test arguments",
        );
      } catch (error) {
        this.setError(error);
        return;
      }
      // "Run" with no arguments would fail any function that takes inputs. Seed
      // sample values from the schema so a fresh function runs out of the box.
      if (Object.keys(argumentsPayload).length === 0) {
        const sample = this._sampleArgsFor(tool);
        if (Object.keys(sample).length > 0) {
          argumentsPayload = sample;
          tool.test_arguments = JSON.stringify(sample, null, 2);
          this.notify("Filled test arguments from schema for this run.", "info");
        }
      }
      this._setToolTestResult(tool.local_id, { running: true });
      try {
        const response = await this.apiRequest(
          `/admin/servers/${encodeURIComponent(serverName)}/tools/${encodeURIComponent(toolName)}/test`,
          {
            method: "POST",
            includeTenant: false,
            body: {
              tenant_id: this.state.tenantId,
              raw_code: tool.raw_code,
              arguments: argumentsPayload,
              requirements: (tool.requirements || "")
                .split("\n")
                .map((line) => line.trim())
                .filter(Boolean),
              action_type: tool.action_type || "read",
              requires_confirmation: Boolean(tool.requires_confirmation),
            },
          },
        );
        this._setToolTestResult(tool.local_id, response);
        if (response.ok) {
          this.notify(`Function '${toolName}' test passed.`, "success");
        } else {
          this.notify(`Function '${toolName}' test failed.`, "warning");
        }
      } catch (error) {
        this._setToolTestResult(tool.local_id, {
          ok: false,
          error:
            error instanceof Error
              ? error.message
              : String(error || "Unknown error"),
        });
        this.setError(error);
      }
    },

    async saveServer() {
      this.clearError();
      try {
        const isCode = this.forms.server.transport === "code";
        if (isCode) {
          const tools = this.forms.server.tools || [];
          if (tools.length === 0) {
            throw new Error("Code servers require at least one function.");
          }
          for (const tool of tools) {
            if (!String(tool.name || "").trim()) {
              throw new Error("Every function needs a name.");
            }
            if (!String(tool.raw_code || "").trim()) {
              throw new Error(
                `Function '${tool.name || "unnamed"}' needs Python source.`,
              );
            }
          }
          // Hard gate: run the authoritative server validator on every function
          // before we attempt the save, so a broken tool is caught here with
          // line-accurate messages instead of failing the whole POST opaquely.
          const validations = await Promise.all(
            tools.map((tool) => this.validateTool(tool)),
          );
          const blockers = [];
          tools.forEach((tool, index) => {
            const issues = (validations[index]?.issues || []).filter(
              (issue) => issue.severity === "error",
            );
            for (const issue of issues) {
              const where = issue.line ? ` (line ${issue.line})` : "";
              blockers.push(
                `'${String(tool.name || "unnamed")}': ${issue.message}${where}`,
              );
            }
          });
          if (blockers.length > 0) {
            this.forms.server.tools.forEach((tool) => {
              if (this.toolErrorCount(tool) > 0) tool.expanded = true;
            });
            this.$nextTick(() => this.refreshCodeEditors());
            throw new Error(
              `Fix ${blockers.length} validation ${blockers.length === 1 ? "issue" : "issues"} before saving — ` +
                blockers.slice(0, 4).join("; ") +
                (blockers.length > 4 ? "; …" : ""),
            );
          }
        }
        const payload = {
          tenant_id: this.state.tenantId,
          server: this.forms.server.server,
          transport: this.forms.server.transport,
          endpoint: isCode ? null : this.forms.server.endpoint || null,
          command: isCode ? null : this.forms.server.command || null,
          metadata: this.parseMetadata(this.forms.server.metadata),
        };
        if (isCode) {
          payload.tools = this.buildCodeTools();
        }
        const serverName = this.forms.server.server;
        await this.apiRequest("/admin/servers", {
          method: "POST",
          body: payload,
        });
        await this.loadServers();
        const saved = this.state.servers.find(
          (item) => String(item.server || "").trim() === String(serverName || "").trim(),
        );
        if (saved) {
          await this.openServerWorkspace(saved, "tools");
        } else {
          this.state.serverComposerOpen = false;
          this.state.serverComposerMode = "create";
        }
        this.notify(`Server '${serverName}' saved.`);
      } catch (error) {
        this.setError(error);
      }
    },

    async toggleServer(server) {
      this.clearError();
      try {
        const willEnable = !server.enabled;
        await this.apiRequest(
          `/admin/servers/${encodeURIComponent(server.server)}`,
          {
            method: "PATCH",
            body: { tenant_id: this.state.tenantId, enabled: willEnable },
          },
        );
        await this.loadServers();
        this.notify(
          `Server '${server.server}' ${willEnable ? "enabled" : "disabled"}.`,
        );
      } catch (error) {
        this.setError(error);
      }
    },

    async deleteServer(serverName) {
      if (!window.confirm(`Delete server '${serverName}'?`)) return;
      this.clearError();
      try {
        await this.apiRequest(
          `/admin/servers/${encodeURIComponent(serverName)}`,
          {
            method: "DELETE",
          },
        );
        await this.loadServers();
        if (String(this.state.serverWorkspace.server || "") === String(serverName || "")) {
          this.closeServerWorkspace();
        }
        this.notify(`Server '${serverName}' deleted.`, "warning");
      } catch (error) {
        this.setError(error);
      }
    },

    async loadCatalog() {
      this.clearError();
      try {
        this.state.catalog = await this.apiRequest("/admin/catalog");
        const expanded = { ...(this.state.catalogExpanded || {}) };
        for (const group of this.catalogGroups()) {
          if (!(group.server in expanded)) {
            expanded[group.server] = true;
          }
        }
        this.state.catalogExpanded = expanded;
      } catch (error) {
        this.setError(error);
      }
    },

    catalogGroups() {
      const map = new Map();
      for (const item of this.state.catalog?.items || []) {
        const key = String(item.server || "unknown");
        if (!map.has(key)) map.set(key, []);
        map.get(key).push(item);
      }
      return Array.from(map.entries())
        .map(([server, items]) => ({
          server,
          items: items.sort((a, b) =>
            String(a.name || "").localeCompare(String(b.name || "")),
          ),
        }))
        .sort((a, b) => a.server.localeCompare(b.server));
    },

    filteredCatalogGroups() {
      const query = String(this.forms.catalog.query || "")
        .trim()
        .toLowerCase();
      if (!query) return this.catalogGroups();
      return this.catalogGroups()
        .map((group) => ({
          server: group.server,
          items: group.items.filter((item) => {
            const haystack = [
              item.server,
              item.name,
              item.description,
              ...(item.scopes || []),
              item.action_type || "",
              item.transport || "",
            ]
              .join(" ")
              .toLowerCase();
            return haystack.includes(query);
          }),
        }))
        .filter((group) => group.items.length > 0);
    },

    toggleCatalogServer(server) {
      this.state.catalogExpanded = {
        ...(this.state.catalogExpanded || {}),
        [server]: !this.state.catalogExpanded?.[server],
      };
    },

    isCatalogServerOpen(server) {
      return this.state.catalogExpanded?.[server] !== false;
    },

    async openCatalogTool(item) {
      await this.openServerWorkspace({
        server: item.server,
        transport: item.transport || "code",
        endpoint: null,
        command: null,
        metadata: {},
      });
      this.notify(`Opened '${item.server}' in Functions Studio.`, "info");
    },

    async loadTelemetry() {
      this.clearError();
      try {
        this.state.telemetry = await this.apiRequest("/admin/telemetry");
        // The time-bucketed trend that backs the volume/latency charts. Scoped
        // by role on the server (platform-admin = cross-tenant merge).
        this.state.analytics.telemetryTrend = await this.apiRequest(
          "/admin/analytics/telemetry-trend?hours=24",
          { includeTenant: !this.isPlatformAdmin() },
        );
        this.$nextTick(() => this.renderTelemetryCharts());
      } catch (error) {
        this.setError(error);
      }
    },

    telemetryStatusKind(status) {
      const s = String(status || "").toLowerCase();
      if (/success|_hit|consumed|approved|resumed|updated|\bok\b/.test(s)) {
        return "ok";
      }
      if (
        /error|fail|forbidden|denied|timeout|quota|suspend|unknown|invalid|disabled|pending/.test(
          s,
        )
      ) {
        return "error";
      }
      return "neutral";
    },

    telemetryStatusLabel(status) {
      return String(status || "—").replace(/_/g, " ");
    },

    telemetrySummary() {
      const items = this.state.telemetry?.items || [];
      const total = items.length;
      let errors = 0;
      const latencies = [];
      for (const item of items) {
        if (this.telemetryStatusKind(item.status) === "error") errors += 1;
        if (typeof item.latency_ms === "number" && isFinite(item.latency_ms)) {
          latencies.push(item.latency_ms);
        }
      }
      latencies.sort((a, b) => a - b);
      const avg = latencies.length
        ? latencies.reduce((a, b) => a + b, 0) / latencies.length
        : null;
      const p95 = latencies.length
        ? latencies[
            Math.min(latencies.length - 1, Math.floor(latencies.length * 0.95))
          ]
        : null;
      const successRate = total
        ? Math.round(((total - errors) / total) * 100)
        : null;
      return { total, errors, avg, p95, successRate };
    },

    formatMs(value) {
      if (value == null || !isFinite(value)) return "—";
      return `${Number(value).toFixed(value < 10 ? 1 : 0)}ms`;
    },

    async runSearch() {
      this.clearError();
      const serverName = String(this.state.serverWorkspace.server || "").trim();
      if (!serverName) {
        this.setError("Select an MCP server before using Search.");
        return;
      }
      try {
        const payload = await this.apiRequest("/admin/search", {
          method: "POST",
          includeTenant: false,
          body: {
            tenant_id: this.state.tenantId,
            server: serverName,
            query: this.forms.search.query,
            mode: this.forms.search.mode,
            limit: this.forms.search.limit,
          },
        });
        this.state.searchResults = payload.items || [];
        this.notify(
          `${this.state.searchResults.length} match(es) found.`,
          "info",
        );
      } catch (error) {
        this.setError(error);
      }
    },

    setEmbeddingScope(scope) {
      const next = scope === "tenant" ? "tenant" : "platform";
      this.state.embeddingScope = next;
      if (next === "tenant" && !this.state.tenantEmbedding) {
        this.loadTenantEmbedding();
      }
    },

    providerNeedsApiKey(provider) {
      return ["openai", "azure_openai", "voyage", "gemini"].includes(provider);
    },

    providerIsAzure(provider) {
      return provider === "azure_openai";
    },

    embeddingNeedsApiKey() {
      return this.providerNeedsApiKey(this.forms.embedding.provider);
    },

    embeddingIsAzure() {
      return this.providerIsAzure(this.forms.embedding.provider);
    },

    embeddingIsRunning() {
      return this.state.embeddingStatus?.state === "running";
    },

    _embeddingPayloadFrom(form) {
      const payload = { provider: form.provider };
      if (form.model) payload.model = form.model;
      if (form.base_url) payload.base_url = form.base_url;
      // Only send api_key when the operator typed one; blank preserves the stored key.
      if (form.api_key) payload.api_key = form.api_key;
      if (this.providerIsAzure(form.provider)) {
        if (form.azure_endpoint) payload.azure_endpoint = form.azure_endpoint;
        if (form.azure_api_version)
          payload.azure_api_version = form.azure_api_version;
        if (form.azure_deployment)
          payload.azure_deployment = form.azure_deployment;
      }
      return payload;
    },

    _embeddingPayload() {
      return this._embeddingPayloadFrom(this.forms.embedding);
    },

    async loadEmbedding() {
      this.clearError();
      try {
        this.state.embedding = await this.apiRequest("/admin/embedding", {
          includeTenant: false,
        });
        this.state.embeddingStatus = this.state.embedding.reprovision || null;
        const cfg = this.state.embedding;
        this.forms.embedding.provider = cfg.provider || "ollama";
        this.forms.embedding.model = cfg.model || "";
        this.forms.embedding.base_url = cfg.base_url || "";
        this.forms.embedding.azure_endpoint = cfg.azure_endpoint || "";
        this.forms.embedding.azure_api_version = cfg.azure_api_version || "";
        this.forms.embedding.azure_deployment = cfg.azure_deployment || "";
        this.forms.embedding.api_key = "";
        if (this.embeddingIsRunning()) this._startEmbeddingPoll();
      } catch (error) {
        this.setError(error);
      }
    },

    async testEmbedding() {
      this.clearError();
      this.state.embeddingTest = null;
      try {
        this.state.embeddingTest = await this.apiRequest(
          "/admin/embedding/test",
          {
            method: "POST",
            includeTenant: false,
            body: this._embeddingPayload(),
          },
        );
      } catch (error) {
        this.setError(error);
      }
    },

    async applyEmbedding() {
      this.clearError();
      this.state.embeddingTest = null;
      this.state.embeddingSaving = true;
      try {
        const payload = this._embeddingPayload();
        payload.reprovision = this.forms.embedding.reprovision;
        this.state.embedding = await this.apiRequest("/admin/embedding", {
          method: "PUT",
          includeTenant: false,
          body: payload,
        });
        this.state.embeddingStatus = this.state.embedding.reprovision || null;
        this.forms.embedding.api_key = "";
        if (this.embeddingIsRunning()) this._startEmbeddingPoll();
        this.notify(
          this.embeddingIsRunning()
            ? "Platform embedding saved — re-embedding in progress."
            : "Platform embedding saved.",
        );
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.embeddingSaving = false;
      }
    },

    async refreshEmbeddingStatus() {
      try {
        this.state.embeddingStatus = await this.apiRequest(
          "/admin/embedding/status",
          {
            includeTenant: false,
          },
        );
      } catch (error) {
        this.setError(error);
      }
    },

    _startEmbeddingPoll() {
      if (this._embeddingPoll) return;
      this._embeddingPoll = setInterval(async () => {
        await this.refreshEmbeddingStatus();
        if (!this.embeddingIsRunning()) {
          clearInterval(this._embeddingPoll);
          this._embeddingPoll = null;
          await this.loadEmbedding();
        }
      }, 3000);
    },

    // ---- Per-tenant embeddings (BYO embeddings, encrypted per tenant) ------- //
    tenantEmbeddingNeedsApiKey() {
      return this.providerNeedsApiKey(this.forms.tenantEmbedding.provider);
    },

    tenantEmbeddingIsAzure() {
      return this.providerIsAzure(this.forms.tenantEmbedding.provider);
    },

    tenantEmbeddingIsRunning() {
      return this.state.tenantEmbeddingStatus?.state === "running";
    },

    // True when this tenant has its own override; false when it inherits the
    // platform default (source === "platform-default" / "env").
    tenantEmbeddingIsOverride() {
      return this.state.tenantEmbedding?.source === "tenant-db";
    },

    _tenantEmbeddingBasePath() {
      return `/admin/tenants/${encodeURIComponent(this.state.tenantId)}/embedding`;
    },

    async loadTenantEmbedding() {
      this.clearError();
      this.state.tenantEmbeddingTest = null;
      try {
        this.state.tenantEmbedding = await this.apiRequest(
          this._tenantEmbeddingBasePath(),
          {
            includeTenant: false,
          },
        );
        this.state.tenantEmbeddingStatus =
          this.state.tenantEmbedding.reprovision || null;
        const cfg = this.state.tenantEmbedding;
        this.forms.tenantEmbedding.provider = cfg.provider || "ollama";
        this.forms.tenantEmbedding.model = cfg.model || "";
        this.forms.tenantEmbedding.base_url = cfg.base_url || "";
        this.forms.tenantEmbedding.azure_endpoint = cfg.azure_endpoint || "";
        this.forms.tenantEmbedding.azure_api_version =
          cfg.azure_api_version || "";
        this.forms.tenantEmbedding.azure_deployment =
          cfg.azure_deployment || "";
        this.forms.tenantEmbedding.api_key = "";
        if (this.tenantEmbeddingIsRunning()) this._startTenantEmbeddingPoll();
      } catch (error) {
        this.setError(error);
      }
    },

    async testTenantEmbedding() {
      this.clearError();
      this.state.tenantEmbeddingTest = null;
      try {
        this.state.tenantEmbeddingTest = await this.apiRequest(
          `${this._tenantEmbeddingBasePath()}/test`,
          {
            method: "POST",
            includeTenant: false,
            body: this._embeddingPayloadFrom(this.forms.tenantEmbedding),
          },
        );
      } catch (error) {
        this.setError(error);
      }
    },

    async applyTenantEmbedding() {
      this.clearError();
      this.state.tenantEmbeddingTest = null;
      this.state.tenantEmbeddingSaving = true;
      try {
        const payload = this._embeddingPayloadFrom(this.forms.tenantEmbedding);
        payload.reprovision = this.forms.tenantEmbedding.reprovision;
        this.state.tenantEmbedding = await this.apiRequest(
          this._tenantEmbeddingBasePath(),
          {
            method: "PUT",
            includeTenant: false,
            body: payload,
          },
        );
        this.state.tenantEmbeddingStatus =
          this.state.tenantEmbedding.reprovision || null;
        this.forms.tenantEmbedding.api_key = "";
        if (this.tenantEmbeddingIsRunning()) this._startTenantEmbeddingPoll();
        this.notify(`Embedding override saved for '${this.state.tenantId}'.`);
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.tenantEmbeddingSaving = false;
      }
    },

    async resetTenantEmbedding() {
      if (
        !window.confirm(
          `Reset '${this.state.tenantId}' to the platform default? ` +
            "This deletes its embedding override.",
        )
      ) {
        return;
      }
      this.clearError();
      this.state.tenantEmbeddingTest = null;
      this.state.tenantEmbeddingSaving = true;
      try {
        const reprovision = this.forms.tenantEmbedding.reprovision
          ? "true"
          : "false";
        await this.apiRequest(
          `${this._tenantEmbeddingBasePath()}?reprovision=${reprovision}`,
          { method: "DELETE", includeTenant: false },
        );
        // Reload so the cards, badge, and form fields reflect the now-inherited
        // platform default, and pick up any reprovision that was kicked off.
        await this.loadTenantEmbedding();
        this.notify(
          `'${this.state.tenantId}' reset to the platform default.`,
          "warning",
        );
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.tenantEmbeddingSaving = false;
      }
    },

    async refreshTenantEmbeddingStatus() {
      try {
        this.state.tenantEmbeddingStatus = await this.apiRequest(
          `${this._tenantEmbeddingBasePath()}/status`,
          { includeTenant: false },
        );
      } catch (error) {
        this.setError(error);
      }
    },

    _startTenantEmbeddingPoll() {
      if (this._tenantEmbeddingPoll) return;
      this._tenantEmbeddingPoll = setInterval(async () => {
        await this.refreshTenantEmbeddingStatus();
        if (!this.tenantEmbeddingIsRunning()) {
          clearInterval(this._tenantEmbeddingPoll);
          this._tenantEmbeddingPoll = null;
          await this.loadTenantEmbedding();
        }
      }, 3000);
    },

    // ====================================================================== //
    //  Analytics (charts + KPIs)                                             //
    // ====================================================================== //
    // Platform-admins get the cross-tenant (platform) scope; everyone else is
    // confined to their own tenant. A tenant-scoped call carries tenant_id (via
    // includeTenant); the platform scope omits it so the server rolls up all.
    _analyticsOpts() {
      return { includeTenant: !this.isPlatformAdmin() };
    },

    async loadAnalytics() {
      this.state.analyticsLoading = true;
      try {
        const opts = this._analyticsOpts();
        const [overview, usageTrend, topTools, quota] = await Promise.all([
          this.apiRequest("/admin/analytics/overview", opts),
          this.apiRequest("/admin/analytics/usage-trend?months=6", opts),
          this.apiRequest("/admin/analytics/top-tools?limit=8", opts),
          this.apiRequest("/admin/analytics/quota-utilization", opts),
        ]);
        this.state.analytics.overview = overview;
        this.state.analytics.usageTrend = usageTrend;
        this.state.analytics.topTools = topTools;
        this.state.analytics.quota = quota;
        this._tweenMetric("calls", Number(overview?.calls || 0));
        this._tweenMetric(
          "sandbox",
          Math.round(Number(overview?.sandbox_ms || 0) / 1000),
        );
        this.$nextTick(() => this.renderDashboardCharts());
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.analyticsLoading = false;
      }
    },

    // Count-up tween: ease state.displayMetrics[key] from its current value to
    // `to` over ~700ms. Cancels any in-flight tween for the same key, and snaps
    // instantly when the user prefers reduced motion.
    _tweenMetric(key, to) {
      const target = Number.isFinite(to) ? to : 0;
      if (this._metricTweens[key]) {
        cancelAnimationFrame(this._metricTweens[key]);
        delete this._metricTweens[key];
      }
      const reduce =
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const from = Number(this.state.displayMetrics[key] || 0);
      if (reduce || from === target) {
        this.state.displayMetrics[key] = target;
        return;
      }
      const duration = 700;
      const start = performance.now();
      const step = (now) => {
        const t = Math.min(1, (now - start) / duration);
        // easeOutCubic — fast then settling, matching --ease-out's feel.
        const eased = 1 - Math.pow(1 - t, 3);
        this.state.displayMetrics[key] = from + (target - from) * eased;
        if (t < 1) {
          this._metricTweens[key] = requestAnimationFrame(step);
        } else {
          this.state.displayMetrics[key] = target;
          delete this._metricTweens[key];
        }
      };
      this._metricTweens[key] = requestAnimationFrame(step);
    },

    // ---- Beta-headroom / confirmation KPIs (derived from overview) --------- //
    betaHeadroom() {
      const o = this.state.analytics.overview || {};
      const max = Number(o.self_registration_max_tenants || 0);
      const used = Number(o.self_registered_count || 0);
      const remaining = max > 0 ? Math.max(0, max - used) : null;
      const pct = max > 0 ? Math.round((used / max) * 100) : null;
      return { max, used, remaining, pct };
    },

    awaitingConfirmationCount() {
      return Number(this.state.analytics.overview?.unconfirmed_count || 0);
    },

    // ---- Chart.js plumbing ------------------------------------------------- //
    _chartColors() {
      // Tuned to read on both the dark and light themes (mid-tone text/grid).
      return {
        text: "#94a3b8",
        grid: "rgba(148, 163, 184, 0.16)",
        accent: "#34d399",
        accentFill: "rgba(52, 211, 153, 0.18)",
        blue: "#60a5fa",
        blueFill: "rgba(96, 165, 250, 0.18)",
        amber: "#fbbf24",
        rose: "#fb7185",
        roseFill: "rgba(251, 113, 133, 0.20)",
        violet: "#a78bfa",
      };
    },

    // Destroy-before-recreate: Chart.js keeps a registry per <canvas>, so re-
    // rendering without disposing the old instance overlays charts and leaks.
    _mountChart(canvasId, config) {
      if (typeof window.Chart === "undefined") return;
      const canvas = document.getElementById(canvasId);
      if (!canvas) return;
      if (this._charts[canvasId]) {
        this._charts[canvasId].destroy();
        delete this._charts[canvasId];
      }
      this._charts[canvasId] = new window.Chart(canvas.getContext("2d"), config);
    },

    _axisOptions() {
      const c = this._chartColors();
      return {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            labels: { color: c.text, boxWidth: 12, font: { size: 11 } },
          },
        },
        scales: {
          x: { ticks: { color: c.text, font: { size: 10 } }, grid: { color: c.grid } },
          y: {
            beginAtZero: true,
            ticks: { color: c.text, font: { size: 10 } },
            grid: { color: c.grid },
          },
        },
      };
    },

    renderDashboardCharts() {
      if (typeof window.Chart === "undefined") return;
      const c = this._chartColors();

      // Usage trend (calls + sandbox seconds per period).
      const trend = this.state.analytics.usageTrend?.points || [];
      this._mountChart("chart-usage-trend", {
        type: "line",
        data: {
          labels: trend.map((p) => p.period),
          datasets: [
            {
              label: "Calls",
              data: trend.map((p) => p.calls),
              borderColor: c.accent,
              backgroundColor: c.accentFill,
              fill: true,
              tension: 0.3,
              yAxisID: "y",
            },
            {
              label: "Sandbox (s)",
              data: trend.map((p) => Math.round((p.sandbox_ms || 0) / 1000)),
              borderColor: c.blue,
              backgroundColor: c.blueFill,
              fill: true,
              tension: 0.3,
              yAxisID: "y1",
            },
          ],
        },
        options: {
          ...this._axisOptions(),
          scales: {
            ...this._axisOptions().scales,
            y1: {
              beginAtZero: true,
              position: "right",
              ticks: { color: c.text, font: { size: 10 } },
              grid: { drawOnChartArea: false },
            },
          },
        },
      });

      // Calls sparkline (KPI card): minimal line, no axes/legend.
      this._mountChart("spark-calls", {
        type: "line",
        data: {
          labels: trend.map((p) => p.period),
          datasets: [
            {
              data: trend.map((p) => p.calls),
              borderColor: c.accent,
              backgroundColor: c.accentFill,
              fill: true,
              tension: 0.4,
              pointRadius: 0,
              borderWidth: 2,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false }, tooltip: { enabled: false } },
          scales: { x: { display: false }, y: { display: false } },
        },
      });

      // Top tools (horizontal bar).
      const tools = this.state.analytics.topTools?.tools || [];
      this._mountChart("chart-top-tools", {
        type: "bar",
        data: {
          labels: tools.map((t) => `${t.server}/${t.tool}`),
          datasets: [
            {
              label: "Calls",
              data: tools.map((t) => t.calls),
              backgroundColor: c.violet,
              borderRadius: 4,
            },
          ],
        },
        options: { ...this._axisOptions(), indexAxis: "y" },
      });

      // Top servers (bar).
      const servers = this.state.analytics.topTools?.servers || [];
      this._mountChart("chart-top-servers", {
        type: "bar",
        data: {
          labels: servers.map((s) => s.server),
          datasets: [
            {
              label: "Calls",
              data: servers.map((s) => s.calls),
              backgroundColor: c.blue,
              borderRadius: 4,
            },
          ],
        },
        options: { ...this._axisOptions() },
      });

      // Quota utilization (platform-admin cross-tenant; bar of calls %).
      const quota = this.state.analytics.quota?.tenants || [];
      this._mountChart("chart-quota", {
        type: "bar",
        data: {
          labels: quota.map((q) => q.tenant_id),
          datasets: [
            {
              label: "Calls used %",
              data: quota.map((q) => q.calls_utilization_pct ?? 0),
              backgroundColor: quota.map((q) =>
                (q.calls_utilization_pct ?? 0) >= 80 ? c.rose : c.accent,
              ),
              borderRadius: 4,
            },
          ],
        },
        options: { ...this._axisOptions() },
      });
    },

    renderTelemetryCharts() {
      if (typeof window.Chart === "undefined") return;
      const c = this._chartColors();
      const points = this.state.analytics.telemetryTrend?.points || [];
      const labels = points.map((p) =>
        new Date(p.bucket).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        }),
      );

      this._mountChart("chart-telemetry-volume", {
        type: "line",
        data: {
          labels,
          datasets: [
            {
              label: "Total",
              data: points.map((p) => p.total),
              borderColor: c.blue,
              backgroundColor: c.blueFill,
              fill: true,
              tension: 0.3,
            },
            {
              label: "Errors",
              data: points.map((p) => p.errors),
              borderColor: c.rose,
              backgroundColor: c.roseFill,
              fill: true,
              tension: 0.3,
            },
          ],
        },
        options: this._axisOptions(),
      });

      this._mountChart("chart-telemetry-latency", {
        type: "line",
        data: {
          labels,
          datasets: [
            {
              label: "avg ms",
              data: points.map((p) =>
                p.latency_avg_ms == null ? null : Math.round(p.latency_avg_ms),
              ),
              borderColor: c.accent,
              backgroundColor: c.accentFill,
              tension: 0.3,
              spanGaps: true,
            },
            {
              label: "p95 ms",
              data: points.map((p) =>
                p.latency_p95_ms == null ? null : Math.round(p.latency_p95_ms),
              ),
              borderColor: c.amber,
              tension: 0.3,
              spanGaps: true,
            },
          ],
        },
        options: this._axisOptions(),
      });
    },

    // ====================================================================== //
    //  Confirmation tiers (self-service beta accounts)                       //
    // ====================================================================== //
    confirmationOf(tenant) {
      return String(tenant?.confirmation || "confirmed").toLowerCase();
    },

    isUnconfirmed(tenant) {
      return this.confirmationOf(tenant) === "unconfirmed";
    },

    filteredTenants() {
      const rows = this.state.tenants || [];
      if (this.state.tenantFilter === "unconfirmed") {
        return rows.filter((t) => this.isUnconfirmed(t));
      }
      return rows;
    },

    unconfirmedTenantCount() {
      return (this.state.tenants || []).filter((t) => this.isUnconfirmed(t))
        .length;
    },

    async confirmTenant(tenant) {
      const id = String(tenant?.tenant_id || "").trim();
      if (!id) return;
      this.clearError();
      try {
        await this.apiRequest(
          `/admin/tenants/${encodeURIComponent(id)}/confirm`,
          { method: "POST", includeTenant: false, body: {} },
        );
        await this.loadTenants();
        this.notify(`Tenant '${id}' confirmed — caps lifted.`);
      } catch (error) {
        this.setError(error);
      }
    },

    async unconfirmTenant(tenant) {
      const id = String(tenant?.tenant_id || "").trim();
      if (!id) return;
      if (
        !window.confirm(
          `Move '${id}' back to unconfirmed? It will be re-capped to code-only, ` +
            "1 server / 1 tool, and a small quota.",
        )
      ) {
        return;
      }
      this.clearError();
      try {
        await this.apiRequest(
          `/admin/tenants/${encodeURIComponent(id)}/unconfirm`,
          { method: "POST", includeTenant: false, body: {} },
        );
        await this.loadTenants();
        this.notify(`Tenant '${id}' moved to unconfirmed.`, "warning");
      } catch (error) {
        this.setError(error);
      }
    },

    // ====================================================================== //
    //  Usage & quota view                                                    //
    // ====================================================================== //
    async loadUsageView() {
      this.clearError();
      this.state.usageLoading = true;
      const tid = encodeURIComponent(this.state.tenantId);
      try {
        const [usage, events] = await Promise.all([
          this.apiRequest(`/admin/tenants/${tid}/usage`, {
            includeTenant: false,
          }),
          this.apiRequest(`/admin/tenants/${tid}/usage/events?limit=100`, {
            includeTenant: false,
          }),
        ]);
        this.state.usage = usage;
        this.state.usageEvents = events;
        this.state.quotaForm.calls_limit = Number(usage?.quota?.calls_limit || 0);
        this.state.quotaForm.sandbox_seconds_limit = Number(
          usage?.quota?.sandbox_seconds_limit || 0,
        );
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.usageLoading = false;
      }
    },

    usagePct(used, limit) {
      const u = Number(used || 0);
      const l = Number(limit || 0);
      if (l <= 0) return null;
      return Math.min(100, Math.round((u / l) * 100));
    },

    async saveQuota() {
      if (!this.isPlatformAdmin()) return;
      this.clearError();
      this.state.quotaSaving = true;
      const tid = encodeURIComponent(this.state.tenantId);
      try {
        await this.apiRequest(`/admin/tenants/${tid}/quota`, {
          method: "PUT",
          includeTenant: false,
          body: {
            calls_limit: Number(this.state.quotaForm.calls_limit || 0),
            sandbox_seconds_limit: Number(
              this.state.quotaForm.sandbox_seconds_limit || 0,
            ),
          },
        });
        await this.loadUsageView();
        this.notify(`Quota updated for '${this.state.tenantId}'.`);
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.quotaSaving = false;
      }
    },

    usageExportUrl() {
      const tid = encodeURIComponent(this.state.tenantId);
      return `/admin/tenants/${tid}/usage/export?format=csv`;
    },
  };
};
