/** Exercise the actual panel module with HA and DOM boundary fakes. */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../custom_components/tesla_solar_controller/log-panel.js"), "utf8");

function createPanel(callWS) {
  let panelClass;
  const nodes = new Map();
  for (const id of ["#status", "#refresh", "#download", "#log"]) {
    nodes.set(id, { textContent: "", disabled: false, addEventListener() {} });
  }
  const context = {
    HTMLElement: class {
      isConnected = false;
      attachShadow() {
        this.shadowRoot = {
          firstChild: null,
          set innerHTML(value) { this.firstChild = true; },
          querySelector: (id) => nodes.get(id),
        };
      }
    },
    customElements: { get() {}, define(name, cls) { panelClass = cls; } },
    Date, Blob, URL, setTimeout,
  };
  vm.runInNewContext(source, context);
  const panel = new panelClass();
  panel.hass = { callWS };
  // Build the DOM first; load explicitly so the test can await completion.
  panel._hass = null;
  panel.connectedCallback();
  panel._hass = { callWS };
  return { panel, nodes };
}

test("loads through authenticated HA API and renders log text literally", async () => {
  const requests = [];
  const content = '{"event":"mode_changed","mode":"<script>unsafe</script>"}\n';
  const { panel, nodes } = createPanel(async (request) => {
    requests.push(request.type);
    return { filename: "tesla_solar_controller.log", content };
  });
  await panel.load();
  assert.deepEqual(requests, ["tesla_solar_controller/activity_log"]);
  assert.equal(nodes.get("#log").textContent, content);
  assert.equal(nodes.get("#download").disabled, false);
  assert.equal(nodes.get("#refresh").disabled, false);
  await panel.load();
  assert.equal(requests.length, 2);
});

test("shows access errors and keeps controls usable for retry", async () => {
  const { panel, nodes } = createPanel(async () => { throw { message: "Administrator access required" }; });
  await panel.load();
  assert.match(nodes.get("#status").textContent, /Administrator access required/);
  assert.equal(nodes.get("#download").disabled, true);
  assert.equal(nodes.get("#refresh").disabled, false);
});

test("overlapping refreshes make only one request", async () => {
  let finish;
  let requests = 0;
  const { panel } = createPanel(() => {
    requests++;
    return new Promise((resolve) => { finish = resolve; });
  });
  const pending = panel.load();
  await panel.load();
  assert.equal(requests, 1);
  finish({ content: "activity\n" });
  await pending;
  assert.equal(panel._busy, false);
});
