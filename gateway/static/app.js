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
    },
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
      errorMessage: "",
    },

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
  };
};
