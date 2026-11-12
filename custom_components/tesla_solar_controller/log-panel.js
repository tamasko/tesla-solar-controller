/** Read-only HA panel. Log data stays behind the authenticated WebSocket API. */
class TeslaSolarControllerLog extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._content = "";
    this._busy = false;
    this._loaded = false;
  }

  set hass(hass) {
    this._hass = hass;
    if (this.isConnected && !this._loaded) this.load();
  }

  connectedCallback() {
    if (!this.shadowRoot.firstChild) {
      this.shadowRoot.innerHTML = `
        <style>
          :host { display:block; box-sizing:border-box; height:100%; padding:24px;
            color:var(--primary-text-color); background:var(--primary-background-color);
            overflow:auto; font-family:var(--paper-font-body1_-_font-family, sans-serif); }
          h1 { font-size:1.5rem; margin:16px 0 8px; }
          a { color:var(--primary-color); }
          .actions { display:flex; flex-wrap:wrap; gap:12px; margin:16px 0; }
          button { font:inherit; padding:10px 16px; cursor:pointer; border-radius:8px;
            border:1px solid var(--divider-color); background:var(--card-background-color);
            color:var(--primary-text-color); }
          button:disabled { opacity:.5; cursor:default; }
          pre { white-space:pre-wrap; overflow-wrap:anywhere; font-size:12px;
            line-height:1.6; border:1px solid var(--divider-color); border-radius:8px;
            padding:16px; background:var(--card-background-color); }
          #status { color:var(--secondary-text-color); }
        </style>
        <a href="/">Home Assistant</a>
        <h1>Tesla controller activity log</h1>
        <p>Commands, wakes, mode changes, safety decisions and errors. Newest entries are at the bottom.</p>
        <div class="actions">
          <button id="refresh" type="button">Refresh log</button>
          <button id="download" type="button" disabled>Download log file</button>
        </div>
        <p id="status" role="status" aria-live="polite">Loading log…</p>
        <pre id="log" aria-label="Controller activity log"></pre>`;
      this.shadowRoot.querySelector("#refresh").addEventListener("click", () => this.load());
      this.shadowRoot.querySelector("#download").addEventListener("click", () => this.download());
    }
    if (this._hass && !this._loaded) this.load();
  }

  async load() {
    if (!this._hass || this._busy) return;
    this._busy = true;
    this._loaded = true;
    const status = this.shadowRoot.querySelector("#status");
    const refresh = this.shadowRoot.querySelector("#refresh");
    const download = this.shadowRoot.querySelector("#download");
    refresh.disabled = true;
    status.textContent = "Loading log…";
    try {
      const result = await this._hass.callWS({ type: "tesla_solar_controller/activity_log" });
      this._content = result.content;
      // Display as text: log fields can never become executable HTML.
      this.shadowRoot.querySelector("#log").textContent = this._content || "No activity recorded yet.";
      status.textContent = `Updated ${new Date().toLocaleTimeString()} · includes retained rotated files · Refresh loads new entries`;
      download.disabled = !this._content;
    } catch (error) {
      status.textContent = `Unable to load log: ${error.message || error.code || "connection error"}`;
      download.disabled = true;
    } finally {
      this._busy = false;
      refresh.disabled = false;
    }
  }

  download() {
    const url = URL.createObjectURL(new Blob([this._content], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "tesla_solar_controller.log";
    this.shadowRoot.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

if (!customElements.get("tesla-solar-controller-log")) {
  customElements.define("tesla-solar-controller-log", TeslaSolarControllerLog);
}
