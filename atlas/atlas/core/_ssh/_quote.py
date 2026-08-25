"""The `{}`-placeholder command engine for the SSH/remote layer.

This is the controller-side twin of the host runner's engine in
`scripts/lib/atlas/_run.py`. The two `atlas` packages (the host scripts package and
this Frappe-app package) deploy to different machines and never import each other, so
— per the project's "don't import — copy" principle — the ~10-line engine is copied
here rather than shared. Keep the two in sync.

The contract is identical to the host `_substitute`: each literal `{}` in the template
is replaced by `shlex.quote(str(param))`, in order; every other character (notably nft
`{ … }` clauses, though those are host-side) is left untouched; an arity mismatch raises
TypeError.

**The one difference from the host engine: there is NO `_render`/`shlex.split` here.**
A remote command is the line the remote sshd hands to the remote *shell* — it must stay
a STRING, with the quoted params surviving that shell as single tokens (Trap 3 in the
plan). Local `run()` splits to an argv for `shell=False`; remote `run_ssh()` keeps the
string. Same `{}` author syntax, different tail.
"""

import re
import shlex

_HOLE = re.compile(r"\{\}")


def substitute(template: str, params: tuple) -> str:
	"""Replace each literal `{}` with `shlex.quote(str(param))`, in order, leaving every
	other character untouched. Raises TypeError when the number of placeholders doesn't
	match the number of params.

	Arity is checked by counting up front rather than by exhausting an iterator: on
	CPython 3.14 a StopIteration raised inside an re.sub replacement propagates raw (it
	is NOT wrapped in RuntimeError), so a count check is the version-independent contract.
	"""
	holes = _HOLE.findall(template)
	if len(holes) != len(params):
		raise TypeError(f"{template!r}: {len(holes)} {{}} placeholder(s) but {len(params)} param(s)")
	it = iter(params)
	return _HOLE.sub(lambda _m: shlex.quote(str(next(it))), template)
