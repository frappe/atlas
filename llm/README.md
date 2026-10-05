# Agent Tooling Setup

Install the tools that you need. Start a new agent session after you install a skill or plugin.

## Repository guides

Use the [Go code review guide](go-code-review-guide.md) to review and simplify Go changes before handover or commit.

Use the [engineering playbook](engineering-playbook/README.md) for the team workflow, environment access rules, code taste and review checklist. The skill loads automatically in this repository. Its README shows how to install the auto mode rules.

## Frappe skills

Install the [Frappe skill collection](https://github.com/frappe/skills):

```bash
npx skills add frappe/skills
```

Do not use `frappe-app-dev` in this repository.

## Review skills

Install [`grill-me`](https://github.com/mattpocock/skills) for strict code review:

```bash
npx skills@latest add mattpocock/skills --skill=grill-me
```

Use `write-pr-description` to write structured pull request descriptions when the skill is available.

## Focused output

Install [`i-have-adhd`](https://github.com/ayghri/i-have-adhd) for focused, action-first output:

```bash
npx skills add ayghri/i-have-adhd
```

Invoke it with your agent's skill command.
