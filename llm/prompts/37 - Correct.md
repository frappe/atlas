Correct the Research.

Read my notes thoroughly.

Server Provider
- Drop stats
- Provision Modal should ask for inputs using standard fieldtypes
- Form should be uneditable after saving.
- Provision
- SSH Private Key should be directly read from disk. Not stored in the DB. All these fields should be read-only shouldn't change after setup.
- On another note, each provider will have different fields, requiremnts etc.
	- Should implement them as different DocTypes.
	- Have a Provider interface. All providers would implement needed methods.


Server
- Name should be UUID
- Add a user defined title field. This can be human readable name. Linked to the name on the Provider.
- All fields should be read-only
- Move tabs to main tab into section.s
- Remove Recent Tasks. Operations show the links.
- Tasks
	- Show a different dialogue for each script. Needs different inputs.
		- All inputs should be mapped to standard field types
	- Reboot modal shows too much HTML.
	- All scripts should be mapped to an items in Actions dropdown
VM
- Remove Stats section
	- Remove IPv6. It's already shown in the field.
- Move the Networking tab to a section in the main tab.
	- SSH Key should be read only after setting. Should be part of Security section.
- Move Activity from a tab to a section.
- Read only fields. All fields should be read only after setting.
- Change "Description" to "Title". Make it title_field.
- Provision should happen automatically after insert.
- Terminate modal has too much custom HTML. Only ask to type the Title.
- Dangerous actions modal should have red button to confirm
- New Form
	- Remove the yellow panel in the new form.
	- Auto Select Server 

Virtual Machine Image
- Remove Image Name from the list view. ID and description have this information.
- Change Description to Title.
- Move the Image Data tab to the main tab. Too many tabs for not enough information. Put it in a collapsible section.
- Save and Sync to Server are two primary buttons. Both are unnecessary.
	- Image should be immutable after creation
	- Sync to All Servers is okay at this stage. But should be automatically done and tracked in a separate DocType
	- Remove all custom HTML from the sync modal. Only the Server Selection is necessary.  Keep the field
	- Sync to all servers, no selection necessary.
- Remove the Sync Panel. Find a standard framework component
- Remove Sync Status Section. This is already tracked in tasks. Not necessary to show here.
- Description and Disk Size should be read only.

Task
- Show the status field in the list view and at the top of the form. It's the most important information about the task.
- Remove server name from the subject of the task. Just Provision is enough.
- Remove Stats section
	- Remove Sibling Task list. Doesn't seem to be useful
	- Remove Header chips, both these are tracked in fields below.
- Remove Red headline component. Framework might have a standard component for this. Refer to llm/references.
- Move STDOUT STDERR to the main tab. Put it in a collapsible section.

Cross Cutting. Important.
- 1: Don't suppress the * next to the required fields. Keep it. It's good for visual feedback.
- 3: Change the "Description" to a "Title" field everywhere. Use the user-defined title as-is. Don't mix "name". Ignore the list view problem. We'll fix it in the framework.
- 4: Ignore
- 5: Ignore
- 7: Ignore
- 8: Can we use a standard pattern instead of custom html?
- 9: Show everything. We'll later do a permissions iterations over code.
- 10: SSH Private Key should be directly read from disk. Not stored in the DB. All these fields should be read-only shouldn't change after setup.
