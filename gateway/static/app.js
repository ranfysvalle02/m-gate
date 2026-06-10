window.adminConsole = function adminConsole(config) {
  return {
    uiPath: config.uiPath,
    loggedInEmail: config.loggedInEmail,
    navItems: [
      { key: "dashboard", label: "Dashboard" },
      { key: "tenants", label: "Tenants" },
      { key: "users", label: "Users" },
      { key: "approvals", label: "Approvals" },
      { key: "servers", label: "Servers" },
      { key: "catalog", label: "Tool Catalog" },
      { key: "telemetry", label: "Telemetry" },
      { key: "search", label: "Search Playground" },
      { key: "embeddings", label: "Embeddings · Platform" },
      { key: "tenantEmbeddings", label: "Embeddings · Tenant" },
      { key: "account", label: "Account" },
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
        transport: "streamable_http",
        endpoint: "",
        command: "",
        metadata: "{}",
        code: {
          name: "",
          description: "",
          action_type: "read",
          requires_confirmation: false,
          requirements: "",
          raw_code: "",
        },
      },
      search: {
        query: "",
        mode: "hybrid",
        limit: 10,
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
    },
    _embeddingPoll: null,
    _tenantEmbeddingPoll: null,

    async init() {
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
      this.state.errorMessage = error instanceof Error ? error.message : String(error || "");
    },

    clearError() {
      this.state.errorMessage = "";
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
        if (this.activeSection === "servers") await this.loadServers();
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
      try {
        await this.apiRequest("/admin/tenants", {
          method: "POST",
          includeTenant: false,
          body: { tenant_id: this.forms.newTenantId },
        });
        this.forms.newTenantId = "";
        await this.loadTenants();
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
        this.state.userNotice = `Created ${this.forms.user.email}.`;
        this.resetUserForm();
        await this.loadUsers();
      } catch (error) {
        this.setError(error);
      }
    },

    async toggleUserStatus(user) {
      this.clearError();
      try {
        await this.apiRequest(`/admin/users/${encodeURIComponent(user.id)}`, {
          method: "PATCH",
          body: { status: user.status === "active" ? "disabled" : "active" },
        });
        await this.loadUsers();
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

    emptyCodeForm() {
      return {
        name: "",
        description: "",
        action_type: "read",
        requires_confirmation: false,
        requirements: "",
        raw_code: "",
      };
    },

    resetServerForm() {
      this.forms.server = {
        server: "",
        transport: "streamable_http",
        endpoint: "",
        command: "",
        metadata: "{}",
        code: this.emptyCodeForm(),
      };
    },

    async editServer(server) {
      this.forms.server = {
        server: server.server || "",
        transport: server.transport || "streamable_http",
        endpoint: server.endpoint || "",
        command: server.command || "",
        metadata: JSON.stringify(server.metadata || {}),
        code: this.emptyCodeForm(),
      };
      if (server.transport !== "code") return;
      // The list view redacts authored source; fetch the single server to load
      // the decrypted function back into the editor.
      this.clearError();
      try {
        const detail = await this.apiRequest(
          `/admin/servers/${encodeURIComponent(server.server)}`,
        );
        const tool = (detail.tools || [])[0] || {};
        const meta = tool.metadata || {};
        this.forms.server.code = {
          name: tool.name || "",
          description: tool.description || "",
          action_type: meta.action_type || "read",
          requires_confirmation: Boolean(meta.requires_confirmation),
          requirements: (tool.requirements || []).join("\n"),
          raw_code: tool.raw_code || "",
        };
      } catch (error) {
        this.setError(error);
      }
    },

    parseMetadata(raw) {
      if (!raw || !raw.trim()) return {};
      return JSON.parse(raw);
    },

    buildCodeTools() {
      const code = this.forms.server.code || {};
      const requirements = (code.requirements || "")
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
      return [
        {
          server: this.forms.server.server,
          name: code.name,
          description: code.description,
          input_schema: {},
          scopes: [],
          raw_code: code.raw_code,
          requirements,
          metadata: {
            action_type: code.action_type,
            requires_confirmation: Boolean(code.requires_confirmation),
          },
        },
      ];
    },

    async saveServer() {
      this.clearError();
      try {
        const isCode = this.forms.server.transport === "code";
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
        await this.apiRequest("/admin/servers", { method: "POST", body: payload });
        this.resetServerForm();
        await this.loadServers();
      } catch (error) {
        this.setError(error);
      }
    },

    async toggleServer(server) {
      this.clearError();
      try {
        await this.apiRequest(`/admin/servers/${encodeURIComponent(server.server)}`, {
          method: "PATCH",
          body: { tenant_id: this.state.tenantId, enabled: !server.enabled },
        });
        await this.loadServers();
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
      } catch (error) {
        this.setError(error);
      }
    },

    async loadCatalog() {
      this.clearError();
      try {
        this.state.catalog = await this.apiRequest("/admin/catalog");
      } catch (error) {
        this.setError(error);
      }
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
