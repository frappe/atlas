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

# Frappe Setup Wizard — the human front-end of the explicit setup contract
# (atlas/setup.py). `*_requires` ships the slide definitions; `*_stages` maps the
# slide answers onto the Layer-1 setters; `*_complete` runs after all stages commit.
setup_wizard_requires = "/assets/atlas/js/setup_wizard.js"
setup_wizard_stages = "atlas.setup.get_setup_stages"
setup_wizard_complete = "atlas.setup.on_complete"

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
	"Atlas Settings": "public/js/atlas_form_overrides.js",
	"DigitalOcean Settings": "public/js/atlas_form_overrides.js",
	"Scaleway Settings": "public/js/atlas_form_overrides.js",
	"Self-Managed Settings": "public/js/atlas_form_overrides.js",
	"Provider Size": "public/js/atlas_form_overrides.js",
	"Provider Image": "public/js/atlas_form_overrides.js",
	"Virtual Machine": "public/js/atlas_form_overrides.js",
	"Virtual Machine Image": "public/js/atlas_form_overrides.js",
	"Virtual Machine Snapshot": "public/js/atlas_form_overrides.js",
	"Reserved IP": "public/js/atlas_form_overrides.js",
	"VPN Tunnel": "public/js/atlas_form_overrides.js",
	"VPN Peer": "public/js/atlas_form_overrides.js",
	"Firewall": "public/js/atlas_form_overrides.js",
	"Task": "public/js/atlas_form_overrides.js",
	"Route53 Settings": "public/js/atlas_form_overrides.js",
	"PowerDNS Settings": "public/js/atlas_form_overrides.js",
	"Lets Encrypt Settings": "public/js/atlas_form_overrides.js",
	"Root Domain": "public/js/atlas_form_overrides.js",
	"TLS Certificate": "public/js/atlas_form_overrides.js",
	"Central Settings": "public/js/atlas_form_overrides.js",
	"SSH Console": "public/js/atlas_form_overrides.js",
	"SSH Command Log": "public/js/atlas_form_overrides.js",
}

# Note: redirecting `/desk` → `/app/atlas` is non-trivial (Frappe hardcodes
# `/desk` to the multi-app launcher; see `spec/10-desk-ui.md` §"The
# workspace"). Every entry point that matters (sidebar Home button,
# bookmarked `/app/atlas`, login redirect after manual login) already lands
# here, so the residual one-click launcher cost is acceptable.
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}

# The user-facing SPA (formerly served at /dashboard) and the self-serve signup
# on-ramp (/signup → /verify → /site-status) have been retired — Central is now
# the customer-facing front door (spec/16-central.md), driving site creation via
# `atlas.atlas.api.site.create_site`. Operators use Desk (/app/atlas); there is no
# guest web surface. See spec/14-self-serve.md.
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

# Seed the brand denylist (spec/18 Component H) on every migrate, idempotently —
# new labels only, so operator edits survive (a fixture would clobber them).
after_migrate = "atlas.install.after_migrate"

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
# Atlas is operator/Central-facing only (System Manager). End-user identity and
# team membership live in Central (spec/16-central.md), which talks to Atlas as
# the operator via token auth — so there is no end-user row-level scoping here.
# (The owner-scoped `Atlas User` audience and its permission helpers were removed
# when self-serve signup moved to Central; see spec/14-self-serve.md.)

# Document Events
# ---------------
# Report VM lifecycle events to Central (spec/16-central.md § Event reporting).
# Handlers enqueue a background POST gated on Central Settings.enabled, so a site
# without Central configured pays nothing and a delivery failure never blocks the
# operation.

doc_events = {
	"Virtual Machine": {
		"after_insert": [
			"atlas.atlas.central_report.on_vm_after_insert",
			"atlas.atlas.satellite_events.on_vm_after_insert",
		],
		"on_update": [
			"atlas.atlas.central_report.on_vm_update",
			"atlas.atlas.satellite_events.on_vm_update",
		],
		"on_trash": [
			"atlas.atlas.central_report.on_vm_trash",
			"atlas.atlas.satellite_events.on_vm_trash",
		],
	},
	"Site": {
		"after_insert": "atlas.atlas.central_report.on_site_after_insert",
		"on_update": "atlas.atlas.central_report.on_site_update",
	},
	# A Pilot reports AS its backing VM (Central mirrors VMs, not Pilots), so its
	# status change emits a vm.status_changed carrying the login handoff.
	"Pilot": {
		"on_update": "atlas.atlas.central_report.on_pilot_update",
	},
	"Virtual Machine Snapshot": {
		"on_update": "atlas.atlas.central_report.on_snapshot_update",
	},
	"Server": {
		"on_update": "atlas.atlas.central_report.on_server_update",
	},
}

# Scheduled Tasks
# ---------------
# Daily TLS renewal: renew every Active certificate whose expiry is within the
# renewal window (re-issue AND re-push to the region's proxies), mirroring the
# proxy reconcile philosophy — the desired state (a fresh cert on every proxy) is
# continuously restored. See spec/13-tls.md.
#
# NOTE — bench self-service subdomain routing (spec/18) is **one-way push**: the guest
# register/deregister/lists its OWN routes over its egress and the controller writes
# inline (the Subdomain hooks reconcile the proxy). There is deliberately **no
# scheduled SSH pull and no sweeper** — teardown is `VirtualMachine.terminate()` alone
# (Component F, total). Do NOT add a `reconcile_*`/`sweep_*` scheduler entry for it; a
# scheduled scan was removed when the model went push-only (the unit test asserts this
# list carries none).

# NOTE — the cron entry below is a PROVISIONING reconciler, unrelated to the
# spec/18 routing-sweeper prohibition above. `provision()` creates a billing
# vendor box synchronously, then a single fire-and-forget finish_provisioning
# job adopts it; if that job is lost the Server strands pre-Active with a
# paid-for box behind it and nothing notices. This sweep re-drives such rows.
# Do NOT delete it citing the "no sweeper" rule — that rule is scoped to bench
# self-routing, not server provisioning. See spec/03-bootstrapping.md.
scheduler_events = {
	"daily": [
		"atlas.atlas.doctype.tls_certificate.tls_certificate.renew_expiring",
	],
	"cron": {
		"*/1 * * * *": [
			"atlas.atlas.central_report.retry_pending",
		],
		"*/10 * * * *": [
			"atlas.atlas.providers.worker.reconcile_pending_servers",
		],
		"*/2 * * * *": [
			"atlas.atlas.migration.reconcile_migrations",
			"atlas.atlas.export.reconcile_image_exports",
		],
		# Controller-side liveness BACKSTOP for the decentralized mesh. Retiring
		# the centralized `*/5` host-mesh reconcile for the host-local
		# atlas-networkd daemon (spec/31) removed the only controller-side signal
		# that a host's daemon is down — a host whose atlas-networkd is failed /
		# masked / disabled silently drops its VMs off the private mesh with no
		# operator signal. This sweep OBSERVES each Active host with a read-only
		# `systemctl is-active atlas-networkd` probe and FLAGS the unhealthy ones
		# (Error Log + WARNING). It does NOT reconfigure anything, so it does not
		# re-centralize the networking control plane — the mesh stays self-healing;
		# this is a smoke detector, not a thermostat. Same `*/5` cadence the
		# retired reconcile ran on.
		"*/5 * * * *": [
			"atlas.atlas.providers.worker.check_networkd_liveness",
		],
	},
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

# Require all whitelisted methods to have type annotations
require_type_annotated_api_methods = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
