window.adminConsole = function adminConsole(config) {
  return {
    uiPath: config.uiPath,
    loggedInEmail: config.loggedInEmail,
    navItems: [
      { key: "dashboard", label: "Dashboard" },
      { key: "tenants", label: "Tenants" },
      { key: "servers", label: "Servers" },
      { key: "catalog", label: "Tool Catalog" },
      { key: "telemetry", label: "Telemetry" },
      { key: "search", label: "Search Playground" },
      { key: "embeddings", label: "Embeddings" },
      { key: "tenantEmbeddings", label: "Tenant Embeddings" },
      { key: "account", label: "Account" },
    ],
    activeSection: "dashboard",
    forms: {
      newTenantId: "",
      server: {
        server: "",
        transport: "streamable_http",
        endpoint: "",
        command: "",
        metadata: "{}",
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

    switchSection(section) {
      this.activeSection = section;
      this.refreshActiveSection();
    },

    async refreshActiveSection() {
      this.clearError();
      try {
        if (this.activeSection === "dashboard") await this.loadStats();
        if (this.activeSection === "tenants") await this.loadTenants();
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

    async loadServers() {
      this.clearError();
      try {
        const payload = await this.apiRequest("/admin/servers");
        this.state.servers = payload.items || [];
      } catch (error) {
        this.setError(error);
      }
    },

    resetServerForm() {
      this.forms.server = {
        server: "",
        transport: "streamable_http",
        endpoint: "",
        command: "",
        metadata: "{}",
      };
    },

    editServer(server) {
      this.forms.server = {
        server: server.server || "",
        transport: server.transport || "streamable_http",
        endpoint: server.endpoint || "",
        command: server.command || "",
        metadata: JSON.stringify(server.metadata || {}),
      };
    },

    parseMetadata(raw) {
      if (!raw || !raw.trim()) return {};
      return JSON.parse(raw);
    },

    async saveServer() {
      this.clearError();
      try {
        const payload = {
          tenant_id: this.state.tenantId,
          server: this.forms.server.server,
          transport: this.forms.server.transport,
          endpoint: this.forms.server.endpoint || null,
          command: this.forms.server.command || null,
          metadata: this.parseMetadata(this.forms.server.metadata),
        };
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
