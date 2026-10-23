const statusIndicators = {
	Available: "gray",
	Reserved: "blue",
	Attaching: "orange",
	Attached: "green",
	Detaching: "orange",
	Blocked: "red",
};

frappe.listview_settings["Public IP Allocation"] = {
	get_indicator(doc) {
		return [__(doc.status), statusIndicators[doc.status] || "gray", `status,=,${doc.status}`];
	},
};
