app_name = "atlas"
app_title = "Atlas"
app_publisher = "Frappe"
app_description = "Building block of Frappe Cloud V2 for vm management"
app_email = "developers@frappe.io"
app_license = "agpl-3.0"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "atlas",
# 		"logo": "/assets/atlas/logo.png",
# 		"title": "Atlas",
# 		"route": "/atlas",
# 		"has_permission": "atlas.api.permission.has_app_permission"
# 	}
# ]

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
# after_install = "atlas.install.after_install"

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

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

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
		"* * * * * */10": [
			"atlas.vm.doctype.virtual_machine.virtual_machine.reconcile_terminating_virtual_machines",
			"atlas.server.doctype.server_ip_address.server_ip_address.enqueue_pending_ip_address_reconcilation",
			"atlas.server.usage.enqueue_server_syncs",
		],
		# Poll and advance in-progress Machine image uploads every 30 seconds.
		"* * * * * */30": [
			"atlas.vm.core.virtual_machine_image_manager.enqueue_pending_machine_image_transfers",
		],
		"* * * * *": [
			"atlas.server.doctype.server_ssh_task.server_ssh_task.mark_timed_out_ssh_tasks",
			"atlas.vm.doctype.virtual_machine.virtual_machine.reconcile_stale_drafts",
		],
	},
	"hourly": ["atlas.server.usage.delete_old_usage_samples"],
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
# before_request = ["atlas.utils.before_request"]
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

# auth_hooks = [
# 	"atlas.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
