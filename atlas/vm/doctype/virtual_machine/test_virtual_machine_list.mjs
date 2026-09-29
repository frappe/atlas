import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const dialogs = [];
const requests = [];

class Dialog {
  constructor(configuration) {
    dialogs.push(configuration);
  }

  show() {}
}

const context = vm.createContext({
  __: (message) => message,
  has_common: () => true,
  frappe: {
    call: (request) => requests.push(request),
    db: { get_value: async () => ({ message: { title: "Ubuntu" } }) },
    listview_settings: {},
    throw: (message) => {
      throw new Error(message);
    },
    ui: { Dialog },
    user_roles: [],
  },
});
const scriptPath = new URL("virtual_machine_list.js", import.meta.url);
vm.runInContext(await readFile(scriptPath, "utf8"), context, {
  filename: scriptPath.pathname,
});
context.showCreateVirtualMachineDialog();

assert.equal(dialogs.length, 1);
const configuration = dialogs[0];
const encryptionField = configuration.fields.find(
  (field) => field.fieldname === "disk_encryption"
);
assert.equal(encryptionField.fieldtype, "Check");
assert.equal(encryptionField.default, 0);

configuration.primary_action({
  disk_encryption: 1,
  firewall_enabled: 0,
  firewall_rules: [],
  public_ipv4_mode: "none",
  public_ipv6_mode: "none",
});

assert.equal(requests.length, 1);
assert.equal(requests[0].args.request.disk_encryption, true);
console.log("ok - VM create dialog submits disk encryption");
