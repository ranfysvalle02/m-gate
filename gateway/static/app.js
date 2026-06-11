window.adminConsole = function adminConsole(config) {
  return {
    uiPath: config.uiPath,
    loggedInEmail: config.loggedInEmail,
    navItems: [
      { key: "dashboard", label: "Dashboard", icon: "📊" },
      { key: "tenants", label: "Tenants", icon: "🏢" },
      { key: "users", label: "Users", icon: "👥" },
      { key: "approvals", label: "Approvals", icon: "✅" },
      { key: "servers", label: "Servers", icon: "🧰" },
      { key: "catalog", label: "Tool Catalog", icon: "🗂️" },
      { key: "telemetry", label: "Telemetry", icon: "📈" },
      { key: "search", label: "Search Playground", icon: "🔎" },
      { key: "embeddings", label: "Embeddings · Platform", icon: "🧠" },
      { key: "tenantEmbeddings", label: "Embeddings · Tenant", icon: "🪪" },
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
          },
        ],
      },
      search: {
        query: "",
        mode: "hybrid",
        limit: 10,
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
      catalog: { items: [] },
      catalogExpanded: {},
      telemetry: { items: [] },
      searchResults: [],
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
      toolTestResults: {},
      tenantConnectOpen: false,
      tenantConnectTenant: "",
      tenantConnectEgress: null,
      tenantConnectSaving: false,
    },
    _embeddingPoll: null,
    _tenantEmbeddingPoll: null,
    _toastSeq: 0,
    _toolSeq: 1,
    _codeEditors: {},

    async init() {
      this.initTheme();
      try {
        await this.loadWhoAmI();
        await Promise.all([this.loadStats(), this.loadTenants()]);
      } catch (_) {
        // requests already set error/redirect when needed
      }
    },

    async apiRequest(path, options = {}) {
      const method = options.method || "GET";
      const body = options.body || null;
      const includeTenant = options.includeTenant !== false;
      const url = new URL(path, window.location.origin);
      if (includeTenant && this.state.tenantId) {
        url.searchParams.set("tenant_id", this.state.tenantId);
      }
      const headers = { Accept: "application/json", ...(options.headers || {}) };
      if (body !== null) {
        headers["Content-Type"] = "application/json";
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
      const message = error instanceof Error ? error.message : String(error || "");
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
      this.state.toasts.push({ id, message: text, type, icon: icons[type] || icons.info });
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

    async openTenantConnect(tenantId) {
      this.state.tenantConnectTenant = String(tenantId || this.state.tenantId || "").trim();
      this.state.tenantConnectOpen = true;
      await this.loadTenantEgressAllowlist(this.state.tenantConnectTenant);
    },

    closeTenantConnect() {
      this.state.tenantConnectOpen = false;
      this.state.tenantConnectSaving = false;
    },

    rpcEndpoint() {
      return `${window.location.origin}/rpc`;
    },

    authModeLabel() {
      return this.state.whoami?.auth_mode || "disabled";
    },

    recommendedScopes() {
      const scopes = (this.state.whoami?.scopes || []).filter(Boolean);
      if (scopes.length > 0) return scopes.join(",");
      return "weather,readonly";
    },

    tokenMintCommand() {
      const tenantId = this.state.tenantConnectTenant || this.state.tenantId || "local-dev";
      return `python -m scripts.mint_token --tenant-id ${tenantId} --groups weather readonly --roles tool:invoke`;
    },

    rpcCurlSample() {
      const tenantId = this.state.tenantConnectTenant || this.state.tenantId || "local-dev";
      if (this.authModeLabel() === "disabled") {
        return [
          "curl -X POST " + this.rpcEndpoint() + " \\",
          '  -H "Content-Type: application/json" \\',
          `  -H "X-MCP-Scopes: ${this.recommendedScopes()}" \\`,
          "  -d '{",
          '    \"jsonrpc\":\"2.0\",',
          '    \"id\":\"list-1\",',
          '    \"method\":\"tools/list\",',
          "    \"params\":{}",
          "  }'",
        ].join("\n");
      }
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
        this.authModeLabel() === "disabled"
          ? `        \"X-MCP-Scopes\": \"${this.recommendedScopes()}\"`
          : '        \"Authorization\": \"Bearer ${TOKEN}\"',
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
        this.forms.tenantConnect.allowlist = (payload.allowlist || []).join("\n");
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
        this.forms.tenantConnect.allowlist = (payload.allowlist || []).join("\n");
        this.notify(`Updated egress allowlist for '${tenantId}'.`);
      } catch (error) {
        this.setError(error);
      } finally {
        this.state.tenantConnectSaving = false;
      }
    },

    async copyText(text, label = "Snippet") {
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
      } catch (error) {
        this.setError(`Copy failed: ${error.message || error}`);
      }
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
      return ["tenant", "function", "connect"].filter((step) => this.checklistDone(step)).length;
    },

    goToChecklistStep(step) {
      if (step === "tenant") this.switchSection("tenants");
      if (step === "function") this.switchSection("servers");
      if (step === "connect") this.switchSection("search");
    },

    switchSection(section) {
      this.activeSection = section;
      this.refreshActiveSection();
    },

    async refreshActiveSection() {
      this.clearError();
      try {
        if (this.activeSection === "dashboard") await this.loadStats();
        if (this.activeSection === "tenants") await this.loadTenants();
        if (this.activeSection === "users") await this.loadUsers();
        if (this.activeSection === "approvals") await this.loadPendingActions();
        if (this.activeSection === "servers") {
          await this.loadServers();
          this.$nextTick(() => this.refreshCodeEditors());
        }
        if (this.activeSection === "catalog") await this.loadCatalog();
        if (this.activeSection === "telemetry") await this.loadTelemetry();
        if (this.activeSection === "embeddings") await this.loadEmbedding();
        if (this.activeSection === "tenantEmbeddings") await this.loadTenantEmbedding();
      } catch (error) {
        this.setError(error);
      }
    },

    async loadWhoAmI() {
      try {
        this.state.whoami = await this.apiRequest("/admin/whoami", { includeTenant: false });
      } catch (error) {
        this.setError(error);
        throw error;
      }
    },

    async loadStats() {
      this.clearError();
      try {
        this.state.stats = await this.apiRequest("/admin/stats", { includeTenant: false });
      } catch (error) {
        this.setError(error);
      }
    },

    async loadTenants() {
      this.clearError();
      try {
        this.state.tenants = await this.apiRequest("/admin/tenants", { includeTenant: false });
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
        reason = window.prompt(`Suspend tenant '${tenant.tenant_id}'? Optional reason:`, "");
        if (reason === null) return;
      }
      try {
        const action = suspend ? "suspend" : "resume";
        await this.apiRequest(`/admin/tenants/${encodeURIComponent(tenant.tenant_id)}/${action}`, {
          method: "POST",
          includeTenant: false,
          body: suspend ? { reason } : {},
        });
        await this.loadTenants();
        this.notify(`Tenant '${tenant.tenant_id}' ${suspend ? "suspended" : "resumed"}.`);
      } catch (error) {
        this.setError(error);
      }
    },

    roleOptions: [
      { value: "user", label: "User" },
      { value: "tenant-admin", label: "Tenant admin" },
      { value: "platform-admin", label: "Platform admin" },
    ],

    rolesForSelection(selection) {
      if (selection === "platform-admin") return ["platform-admin", "admin"];
      if (selection === "tenant-admin") return ["admin"];
      return ["user"];
    },

    roleLabel(roles) {
      const set = new Set(roles || []);
      if (set.has("platform-admin")) return "Platform admin";
      if (set.has("admin")) return "Tenant admin";
      return (roles || []).join(", ") || "user";
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
        this.notify(`User '${user.email}' ${nextStatus === "active" ? "enabled" : "disabled"}.`);
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
        await this.apiRequest(`/admin/actions/${encodeURIComponent(action.action_id)}/approve`, {
          method: "POST",
        });
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
        await this.apiRequest(`/admin/actions/${encodeURIComponent(action.action_id)}/reject`, {
          method: "POST",
        });
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
      } catch (error) {
        this.setError(error);
      }
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
        requirements: "",
        raw_code: "",
        scopes: "",
        input_schema: "{}",
        test_arguments: "{}",
      };
    },

    normalizeToolForm(tool = {}) {
      const meta = tool.metadata || {};
      const requirements = Array.isArray(tool.requirements) ? tool.requirements.join("\n") : "";
      const scopes = Array.isArray(tool.scopes) ? tool.scopes.join(", ") : "";
      return {
        local_id: typeof tool.local_id === "number" ? tool.local_id : this._nextToolId(),
        name: tool.name || "",
        description: tool.description || "",
        action_type: meta.action_type || "read",
        requires_confirmation: Boolean(meta.requires_confirmation),
        requirements,
        raw_code: tool.raw_code || "",
        scopes,
        input_schema: JSON.stringify(tool.input_schema || {}, null, 2),
        test_arguments: "{}",
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
      this._teardownCodeEditors();
      this.$nextTick(() => this.refreshCodeEditors());
    },

    addToolToServer() {
      this.forms.server.tools.push(this.emptyToolForm());
      this.$nextTick(() => this.refreshCodeEditors());
    },

    removeToolFromServer(localId) {
      if ((this.forms.server.tools || []).length <= 1) {
        this.setError("A code server must include at least one function.");
        return;
      }
      this.forms.server.tools = (this.forms.server.tools || []).filter(
        (tool) => tool.local_id !== localId,
      );
      this._teardownCodeEditor(localId);
      this.$nextTick(() => this.refreshCodeEditors());
    },

    async editServer(server) {
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
        const tools = (detail.tools || []).map((tool) => this.normalizeToolForm(tool));
        this.forms.server.tools = tools.length > 0 ? tools : [this.emptyToolForm()];
        this.state.toolTestResults = {};
        this.$nextTick(() => this.refreshCodeEditors());
      } catch (error) {
        this.setError(error);
      }
    },

    parseMetadata(raw) {
      if (!raw || !raw.trim()) return {};
      return JSON.parse(raw);
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

    async _loadCodeMirror() {
      if (this._cmLib) return this._cmLib;
      if (this._cmLoadPromise) return this._cmLoadPromise;
      this._cmLoadPromise = (async () => {
        const [stateMod, viewMod, commandsMod, languageMod, pythonMod] = await Promise.all([
          import("https://esm.sh/@codemirror/state@6.4.1"),
          import("https://esm.sh/@codemirror/view@6.27.0"),
          import("https://esm.sh/@codemirror/commands@6.7.1"),
          import("https://esm.sh/@codemirror/language@6.10.3"),
          import("https://esm.sh/@codemirror/lang-python@6.1.6"),
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
      return (this.forms.server.tools || []).find((item) => String(item.local_id) === String(toolId));
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
        // CDN/module load failed; textareas remain as fallback editor.
        return;
      }
      const activeIds = new Set(
        (this.forms.server.tools || []).map((tool) => String(tool.local_id)),
      );
      for (const editorId of Object.keys(this._codeEditors)) {
        if (!activeIds.has(String(editorId))) {
          this._teardownCodeEditor(editorId);
        }
      }
      for (const tool of this.forms.server.tools || []) {
        const toolId = String(tool.local_id);
        if (this._codeEditors[toolId]) continue;
        const host = document.querySelector(`[data-code-editor-host="${toolId}"]`);
        const fallback = document.querySelector(`[data-code-editor-fallback="${toolId}"]`);
        if (!host || !fallback) continue;
        fallback.classList.add("code-editor-fallback");
        fallback.style.display = "none";
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
            cm.syntaxHighlighting(cm.defaultHighlightStyle, { fallback: true }),
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
              }
            }),
          ],
        });
        const view = new cm.EditorView({
          state,
          parent: host,
        });
        this._codeEditors[toolId] = view;
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
          },
        };
      });
    },

    lintHintsForTool(tool) {
      const code = String(tool?.raw_code || "");
      const hints = [];
      const bannedImports = /\b(import|from)\s+(os|sys|subprocess|socket|shutil|ctypes|multiprocessing|importlib|marshal|pickle|builtins|resource|signal|pty)\b/;
      if (bannedImports.test(code)) {
        hints.push("Uses an import blocked by sandbox policy.");
      }
      const bannedCalls = /\b(eval|exec|compile|__import__|globals|locals|vars|open|breakpoint)\s*\(/;
      if (bannedCalls.test(code)) {
        hints.push("Uses a blocked function call (eval/exec/open/etc).");
      }
      if (code.length > 64 * 1024) {
        hints.push("Source exceeds the 64KB sandbox limit.");
      }
      if (tool?.name && !new RegExp(`\\bdef\\s+${tool.name}\\s*\\(`).test(code)) {
        hints.push("Function name should match the tool name.");
      }
      return hints;
    },

    hasLintHints(tool) {
      return this.lintHintsForTool(tool).length > 0;
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
        argumentsPayload = this.parseJsonObject(tool.test_arguments, "Test arguments");
      } catch (error) {
        this.setError(error);
        return;
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
          error: error instanceof Error ? error.message : String(error || "Unknown error"),
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
              throw new Error(`Function '${tool.name || "unnamed"}' needs Python source.`);
            }
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
        await this.apiRequest("/admin/servers", { method: "POST", body: payload });
        this.resetServerForm();
        await this.loadServers();
        this.notify(`Server '${serverName}' saved.`);
      } catch (error) {
        this.setError(error);
      }
    },

    async toggleServer(server) {
      this.clearError();
      try {
        const willEnable = !server.enabled;
        await this.apiRequest(`/admin/servers/${encodeURIComponent(server.server)}`, {
          method: "PATCH",
          body: { tenant_id: this.state.tenantId, enabled: willEnable },
        });
        await this.loadServers();
        this.notify(`Server '${server.server}' ${willEnable ? "enabled" : "disabled"}.`);
      } catch (error) {
        this.setError(error);
      }
    },

    async deleteServer(serverName) {
      if (!window.confirm(`Delete server '${serverName}'?`)) return;
      this.clearError();
      try {
        await this.apiRequest(`/admin/servers/${encodeURIComponent(serverName)}`, {
          method: "DELETE",
        });
        await this.loadServers();
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
          items: items.sort((a, b) => String(a.name || "").localeCompare(String(b.name || ""))),
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
      this.activeSection = "servers";
      await this.editServer({
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
      } catch (error) {
        this.setError(error);
      }
    },

    async runSearch() {
      this.clearError();
      try {
        const payload = await this.apiRequest("/admin/search", {
          method: "POST",
          includeTenant: false,
          body: {
            tenant_id: this.state.tenantId,
            query: this.forms.search.query,
            mode: this.forms.search.mode,
            limit: this.forms.search.limit,
          },
        });
        this.state.searchResults = payload.items || [];
        this.notify(`${this.state.searchResults.length} match(es) found.`, "info");
      } catch (error) {
        this.setError(error);
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
        if (form.azure_api_version) payload.azure_api_version = form.azure_api_version;
        if (form.azure_deployment) payload.azure_deployment = form.azure_deployment;
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
        this.state.embeddingTest = await this.apiRequest("/admin/embedding/test", {
          method: "POST",
          includeTenant: false,
          body: this._embeddingPayload(),
        });
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
        this.state.embeddingStatus = await this.apiRequest("/admin/embedding/status", {
          includeTenant: false,
        });
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
        this.state.tenantEmbedding = await this.apiRequest(this._tenantEmbeddingBasePath(), {
          includeTenant: false,
        });
        this.state.tenantEmbeddingStatus = this.state.tenantEmbedding.reprovision || null;
        const cfg = this.state.tenantEmbedding;
        this.forms.tenantEmbedding.provider = cfg.provider || "ollama";
        this.forms.tenantEmbedding.model = cfg.model || "";
        this.forms.tenantEmbedding.base_url = cfg.base_url || "";
        this.forms.tenantEmbedding.azure_endpoint = cfg.azure_endpoint || "";
        this.forms.tenantEmbedding.azure_api_version = cfg.azure_api_version || "";
        this.forms.tenantEmbedding.azure_deployment = cfg.azure_deployment || "";
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
        this.state.tenantEmbedding = await this.apiRequest(this._tenantEmbeddingBasePath(), {
          method: "PUT",
          includeTenant: false,
          body: payload,
        });
        this.state.tenantEmbeddingStatus = this.state.tenantEmbedding.reprovision || null;
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
        const reprovision = this.forms.tenantEmbedding.reprovision ? "true" : "false";
        await this.apiRequest(
          `${this._tenantEmbeddingBasePath()}?reprovision=${reprovision}`,
          { method: "DELETE", includeTenant: false },
        );
        // Reload so the cards, badge, and form fields reflect the now-inherited
        // platform default, and pick up any reprovision that was kicked off.
        await this.loadTenantEmbedding();
        this.notify(`'${this.state.tenantId}' reset to the platform default.`, "warning");
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
  };
};
