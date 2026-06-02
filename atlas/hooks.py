app_name = "atlas"
app_title = "Atlas"
app_publisher = "Frappe"
app_description = "Frappe Hosting Platform"
app_email = "aditya@frappe.io"
app_license = "agpl-3.0"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
add_to_apps_screen = [
	{
		"name": "atlas",
		"logo": "/assets/atlas/images/atlas-logo.svg",
		"title": "Atlas",
		"route": "/app/atlas",
	}
]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
app_include_css = "/assets/atlas/css/atlas_desk.css"
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

doctype_js = {
	"Server": "public/js/atlas_form_overrides.js",
	"Provider": "public/js/atlas_form_overrides.js",
	"Atlas Settings": "public/js/atlas_form_overrides.js",
	"DigitalOcean Settings": "public/js/atlas_form_overrides.js",
	"Self-Managed Settings": "public/js/atlas_form_overrides.js",
	"Provider Size": "public/js/atlas_form_overrides.js",
	"Provider Image": "public/js/atlas_form_overrides.js",
	"Virtual Machine": "public/js/atlas_form_overrides.js",
	"Virtual Machine Image": "public/js/atlas_form_overrides.js",
	"Virtual Machine Snapshot": "public/js/atlas_form_overrides.js",
	"Task": "public/js/atlas_form_overrides.js",
}

# Note: redirecting `/desk` → `/app/atlas` is non-trivial (Frappe hardcodes
# `/desk` to the multi-app launcher; see `spec/10-desk-ui.md` §"The
# workspace"). Every entry point that matters (sidebar Home button,
# bookmarked `/app/atlas`, login redirect after manual login) already lands
# here, so the residual one-click launcher cost is acceptable.
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}

# The user-facing SPA (frappe-ui) is served at /dashboard. The Vue app owns
# every sub-path under it; the host page is atlas/www/dashboard.html, built
# from atlas/frontend. Operators use Desk (/app/atlas); users use this.
# See spec/11-user-ui.md.
website_route_rules = [
	{"from_route": "/dashboard/<path:app_path>", "to_route": "dashboard"},
]
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

# Fixtures
# --------
# The Atlas User role (the SPA's user audience). Scoped to just this role so a
# migrate doesn't sweep every Role on the site. See spec/11-user-ui.md.
fixtures = [
	{"dt": "Role", "filters": [["name", "=", "Atlas User"]]},
]

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

# Permissions
# -----------
# Row-level access for the Atlas User audience (the dashboard SPA). Operators
# (System Manager) are unrestricted; users see only their own machines /
# snapshots, and — for the inline Activity panel — only the Tasks of a machine
# they own. See atlas/atlas/permissions.py and spec/11-user-ui.md.

permission_query_conditions = {
	"Virtual Machine": "atlas.atlas.permissions.owner_only",
	"Virtual Machine Snapshot": "atlas.atlas.permissions.owner_only",
	"Task": "atlas.atlas.permissions.task_by_owned_vm",
}

has_permission = {
	"Task": "atlas.atlas.permissions.task_has_permission",
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

# Require all whitelisted methods to have type annotations
require_type_annotated_api_methods = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
