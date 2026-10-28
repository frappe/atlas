from __future__ import annotations

import re
import shlex

import frappe
from frappe import _

# Read-only programs and the arguments that make them write, change state, or never end.
READ_ONLY_PROGRAMS: dict[str, frozenset[str]] = {
	"cat": frozenset(),
	"column": frozenset(),
	"cut": frozenset(),
	"df": frozenset(),
	"dmesg": frozenset({"-c", "-C", "-D", "-E", "-n", "--clear", "--read-clear", "--console-off", "--console-on", "--console-level"}),
	"du": frozenset(),
	"free": frozenset(),
	"grep": frozenset(),
	"head": frozenset(),
	"iostat": frozenset(),
	"journalctl": frozenset({"-f", "--follow", "--flush", "--rotate", "--sync", "--setup-keys", "--relinquish-var", "--vacuum-size", "--vacuum-time", "--vacuum-files"}),
	"ls": frozenset(),
	"lsblk": frozenset(),
	"lscpu": frozenset(),
	"lsof": frozenset(),
	"mpstat": frozenset(),
	"nproc": frozenset(),
	"ps": frozenset(),
	"sort": frozenset({"-o", "--output"}),
	"ss": frozenset({"-K", "--kill"}),
	"stat": frozenset(),
	"tail": frozenset({"-f", "-F", "--follow"}),
	"tr": frozenset(),
	"uname": frozenset(),
	"uptime": frozenset(),
	"vmstat": frozenset(),
	"wc": frozenset(),
	"zgrep": frozenset(),
}  # fmt: skip

# Programs that can also change state. The first argument must be a read-only subcommand.
READ_ONLY_SUBCOMMANDS: dict[str, frozenset[str]] = {
	"ip": frozenset({"-4", "-6", "-br", "-d", "-j", "-s", "addr", "address", "a", "link", "l", "route", "r", "neigh", "n", "rule", "netns"}),
	"nft": frozenset({"list"}),
	"systemctl": frozenset({"status", "show", "cat", "is-active", "is-enabled", "is-failed", "list-units", "list-timers", "--failed"}),
	"wg": frozenset({"show"}),
	"zfs": frozenset({"list", "get"}),
	"zpool": frozenset({"list", "status", "iostat", "get"}),
}  # fmt: skip

# Words that change state when they follow a read-only subcommand, such as `ip route flush`.
CHANGING_WORDS = frozenset(
	{"add", "append", "attach", "change", "del", "delete", "flush", "replace", "set", "exec"}
)
SEPARATORS = frozenset({";", "&&", "||", "|"})
REFUSED_TEXT = ("`", "$(", "${", "<(", ">(")
SECRET_PATH = re.compile(
	r"shadow|\.ssh/|/etc/wireguard|/etc/metal|private|\.key\b|\.pem\b|/environ\b|\.env\b|credential|secret",
	re.IGNORECASE,
)


def validate_read_only_command(command: str) -> None:
	"""Refuse a command unless each part runs an allowed program with read-only arguments."""
	if not command.strip():
		frappe.throw(_("The command is empty."))
	if any(text in command for text in REFUSED_TEXT):
		frappe.throw(_("Command substitution is not allowed."))

	for words in _split_commands(command):
		_validate_words(words)


def _split_commands(command: str) -> list[list[str]]:
	lexer = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=True)
	lexer.whitespace_split = True
	commands: list[list[str]] = [[]]
	for token in lexer:
		if token in SEPARATORS:
			commands.append([])
		elif set(token) <= set("();<>|&"):
			frappe.throw(_("{0} is not allowed. Use only ;, &&, || and |.").format(token))
		else:
			commands[-1].append(token)
	return [words for words in commands if words]


def _validate_words(words: list[str]) -> None:
	program, arguments = words[0], words[1:]
	if program in READ_ONLY_SUBCOMMANDS:
		subcommands = READ_ONLY_SUBCOMMANDS[program]
		if not arguments or arguments[0] not in subcommands:
			frappe.throw(
				_("Only these {0} subcommands are allowed: {1}.").format(
					program, ", ".join(sorted(subcommands))
				)
			)
		refused_arguments = CHANGING_WORDS
	elif program in READ_ONLY_PROGRAMS:
		refused_arguments = READ_ONLY_PROGRAMS[program]
	else:
		frappe.throw(_("{0} is not a read-only program.").format(program))

	for argument in arguments:
		if argument in refused_arguments or argument.split("=", 1)[0] in refused_arguments:
			frappe.throw(_("The argument {0} is not allowed with {1}.").format(argument, program))
		if SECRET_PATH.search(argument):
			frappe.throw(_("{0} can hold a secret. It is not allowed.").format(argument))
