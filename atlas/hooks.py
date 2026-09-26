app_name = "atlas"
app_title = "Atlas"
app_publisher = "Frappe"
app_description = "Building block of Frappe Cloud V2 for vm management"
app_email = "developers@frappe.io"
app_license = "agpl-3.0"

fixtures = [
	{"dt": "Role", "filters": [["name", "=", "Atlas Admin"]]},
	{"dt": "Role Profile", "filters": [["name", "=", "Atlas Admin"]]},
]


# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
add_to_apps_screen = [
	{
		"name": "atlas",
		"logo": "/assets/atlas/logo.png",
		"title": "Atlas",
		"route": "/desk/atlas",
	}
]

# Navigation lives in the Atlas sidebar. These modules keep their doctypes and stay
# reachable, but they no longer get a dock entry of their own.
code_only_modules = {
	"Metal Server": ["Atlas"],
	"Service": ["Atlas"],
	"VM": ["Atlas"],
}

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/atlas/css/atlas.css"
# app_include_js = "/assets/atlas/js/atlas.js"

# include js, css files in header of web template
# web_include_css = "/assets/atlas/css/atlas.css"
# web_include_js = "/assets/atlas/js/atlas.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "atlas/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "atlas/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "atlas.utils.jinja_methods",
# 	"filters": "atlas.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "atlas.install.before_install"
after_install = [
	"atlas.atlas.core.install.complete_setup_wizard",
	"atlas.atlas.core.host_binaries.publish_host_binaries",
	"atlas.service.core.service_package.publish_service_packages",
]
after_migrate = [
	"atlas.atlas.core.install.realign_scheduled_job_baselines",
	"atlas.atlas.core.host_binaries.publish_host_binaries",
	"atlas.service.core.service_package.publish_service_packages",
]

# Uninstallation
# ------------

# before_uninstall = "atlas.uninstall.before_uninstall"
# after_uninstall = "atlas.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "atlas.utils.before_app_install"
# after_app_install = "atlas.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "atlas.utils.before_app_uninstall"
# after_app_uninstall = "atlas.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "atlas.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "atlas.notifications.get_notification_config"

# Awesome Bar
# -----------
# Extra search results: list of dicts with label, description, route, index.
# route: ["List", "ToDo"], "/desk/docs/some/page", or "https://example.com"
# awesomebar_search = ["atlas.search.awesomebar_results"]

# Permissions
# -----------
# Permissions evaluated in scripted ways

permission_query_conditions = {
	"Public IP Allocation": "atlas.auth.overrides.get_permission_query_conditions",
	"Virtual Machine": "atlas.auth.overrides.get_permission_query_conditions",
	"Virtual Machine Image": "atlas.auth.overrides.get_permission_query_conditions",
	"Virtual Machine Migration": "atlas.auth.overrides.get_permission_query_conditions",
}

has_permission = {
	"Public IP Allocation": "atlas.auth.overrides.has_permission",
	"Virtual Machine": "atlas.auth.overrides.has_permission",
	"Virtual Machine Image": "atlas.auth.overrides.has_permission",
	"Virtual Machine Migration": "atlas.auth.overrides.has_permission",
}

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"atlas.tasks.all"
# 	],
# 	"daily": [
# 		"atlas.tasks.daily"
# 	],
# 	"hourly": [
# 		"atlas.tasks.hourly"
# 	],
# 	"weekly": [
# 		"atlas.tasks.weekly"
# 	],
# 	"monthly": [
# 		"atlas.tasks.monthly"
# 	],
# }

scheduler_events = {
	"cron": {
		"*/5 * * * *": [
			"atlas.auth.jwks.sync_central_jwks",
		],
		"* * * * * */10": [
			"atlas.vm.doctype.virtual_machine.virtual_machine.reconcile_terminating_virtual_machines",
			"atlas.metal_server.doctype.public_ip_pool.public_ip_pool.enqueue_pending_pool_reconciliation",
			"atlas.metal_server.doctype.public_ip_allocation.public_ip_allocation.enqueue_pending_allocation_reconciliation",
			"atlas.metal_server.doctype.public_ip_allocation.public_ip_allocation.enqueue_allocation_moves",
			"atlas.metal_server.usage.enqueue_server_syncs",
		],
		"* * * * * */30": [
			"atlas.vm.core.vm_image_transfer.enqueue_pending_virtual_machine_image_transfers",
			"atlas.vm.core.vm_image_deletion.enqueue_pending_virtual_machine_image_deletions",
		],
		"*/15 * * * *": [
			"atlas.atlas.core.artifacts.delete_unlinked_files",
			"atlas.vm.core.vm_image_storage_migration.enqueue_site_file_image_migrations",
			"atlas.vm.core.vm_image_storage_migration.delete_expired_site_files",
		],
		"0 */12 * * *": [
			"atlas.atlas.doctype.atlas_settings.atlas_settings.rotate_proxy_cluster_password",
		],
		"* * * * *": [
			"atlas.metal_server.core.public_ip_service.replenish_direct_allocations",
			"atlas.atlas.doctype.ssh_task.ssh_task.mark_timed_out_ssh_tasks",
			"atlas.vm.core.vm_migration.reconcile_migrations",
			"atlas.vm.doctype.virtual_machine.virtual_machine.reconcile_stale_drafts",
			"atlas.service.doctype.cargo_server.cargo_server.enqueue_pending_cargo_provisioning",
			"atlas.service.core.cargo.bucket.enqueue_pending_bucket_provisioning",
			"atlas.service.doctype.cargo_server.cargo_server.enqueue_pending_pilot_release_tracker_enable",
			"atlas.service.doctype.proxy_server.proxy_server.enqueue_pending_proxies_provisioning",
			"atlas.service.doctype.ipv6_router_server.ipv6_router_server.enqueue_pending_ipv6_router_provisioning",
			"atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server.enqueue_pending_gateway_provisioning",
			"atlas.service.core.proxy.configuration.reconcile_proxy_configurations",
		],
	},
	"hourly": ["atlas.metal_server.usage.delete_old_usage_samples"],
	"daily": [
		"atlas.atlas.doctype.atlas_settings.atlas_settings.renew_expiring_wildcard_certificate",
		"atlas.metal_server.doctype.metal_server.metal_server.renew_expiring_tls_certificates",
	],
}

# Testing
# -------

# before_tests = "atlas.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "atlas.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "atlas.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "atlas.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
before_request = [
	# Register the Atlas API routes before Frappe matches an API request.
	"atlas.api.router.register_atlas_api"
]
# after_request = ["atlas.utils.after_request"]

# Job Events
# ----------
# before_job = ["atlas.utils.before_job"]
# after_job = ["atlas.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

auth_hooks = ["atlas.auth.request.validate_auth"]

# Automatically update python controller files with type annotations for this app.
export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
