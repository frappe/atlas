import frappe


def run():
	"""Create (or reset) a throwaway Atlas User for browser testing the SPA.

	Idempotent: safe to re-run. Prints the credentials.
	"""
	email = "dashboard.tester@atlas.local"
	password = "Test@12345"

	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
	else:
		user = frappe.new_doc("User")
		user.email = email
		user.first_name = "Dashboard"
		user.last_name = "Tester"
		user.send_welcome_email = 0
		user.insert(ignore_permissions=True)

	user.enabled = 1
	user.new_password = password
	# Strip any roles, then grant only Atlas User — this is the SPA audience.
	user.set("roles", [])
	user.append("roles", {"role": "Atlas User"})
	user.save(ignore_permissions=True)

	frappe.db.commit()
	print(f"ATLAS_DASHBOARD_USER={email}")
	print(f"ATLAS_DASHBOARD_PASS={password}")
