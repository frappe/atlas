const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(
  path.join(__dirname, "../atlas/www/vm_console.html"),
  "utf8"
);
const source = html
  .split("<script>")[1]
  .split("</script>")[0]
  .replace("{{ sitename | tojson }}", '"atlas.localhost"')
  .replace("{{ socketio_port | tojson }}", "13900");

let input;
let output;
let disconnected = 0;
const events = {};
const sent = [];
const elements = {};

vm.runInNewContext(source, {
  URLSearchParams,
  TextEncoder,
  Uint8Array,
  btoa,
  atob,
  window: {
    location: {
      hash: "#token=fixture",
      origin: "https://atlas.fc.test",
      hostname: "atlas.fc.test",
    },
    addEventListener() {},
  },
  document: {
    getElementById(id) {
      return (elements[id] ||= { style: {}, addEventListener() {} });
    },
  },
  Terminal: class {
    cols = 80;
    rows = 24;
    loadAddon() {}
    open() {}
    focus() {}
    onResize() {}
    onData(callback) {
      input = callback;
    }
    write(bytes) {
      output = bytes;
    }
  },
  FitAddon: {
    FitAddon: class {
      fit() {}
    },
  },
  io: (url, options) => {
    assert.equal(url, "https://atlas.fc.test/atlas.localhost");
    assert.equal(
      options.transports,
      undefined,
      "Preserve polling and upgrade negotiation"
    );
    assert.equal(options.reconnection, false);
    assert.equal(options.withCredentials, true);
    return {
      on(name, callback) {
        events[name] = callback;
      },
      emit(...args) {
        sent.push(args);
      },
      disconnect() {
        disconnected++;
        events.disconnect();
      },
    };
  },
});

for (const text of [
  "ls\r",
  "café",
  "हिन्दी 🛠️",
  "\x1b[A",
  "line one\nline two\n",
]) {
  input(text);
  assert.equal(sent.at(-1)[0], "atlas_console_input");
  assert.equal(Buffer.from(sent.at(-1)[1], "base64").toString("utf8"), text);
}
events.atlas_console_output(Buffer.from("café 🛠️").toString("base64"));
assert.equal(Buffer.from(output).toString("utf8"), "café 🛠️");

events.atlas_console_error("private server error");
events.disconnect();
assert.equal(disconnected, 1);
assert.equal(
  elements["atlas-overlay-title"].textContent,
  "Console unavailable"
);
assert.equal(elements["atlas-overlay"].style.display, "flex");
console.log(
  "PASS UTF-8 input/output, control sequences, multiline paste, and idempotent disconnect"
);
