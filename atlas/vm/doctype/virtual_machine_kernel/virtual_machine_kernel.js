// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Virtual Machine Kernel", {
	refresh(frm) {
		frm.add_custom_button(__("Download Kernel"), () => {
			frm.call("get_download_link").then(({ message }) => {
				const link = document.createElement("a");
				link.href = message;
				link.download = "";
				document.body.appendChild(link);
				link.click();
				link.remove();
			});
		});
	},
});
