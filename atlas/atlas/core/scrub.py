from __future__ import annotations

import math
import re
from collections import Counter

REDACTED = "[REDACTED]"
SECRET_NAMES = r"password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|privatekey|presharedkey|credential"

# Each pattern stays on one line, so scrubbing complete lines of a stream gives the same text as
# scrubbing the whole output at the end.
SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
	(re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"), "[REDACTED:jwt]"),
	(re.compile(r"(?i)\b(bearer|basic)([ \t]+)[\w.~+/=-]{8,}"), rf"\1\2{REDACTED}"),
	(re.compile(r"\b(?:sk|pk|rk)-[\w-]{16,}"), "[REDACTED:key]"),
	(re.compile(r"\b(?:gh[pousr]|github_pat)_\w{20,}"), "[REDACTED:key]"),
	(re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED:key]"),
	(re.compile(r"\bxox[abprs]-[\w-]{10,}"), "[REDACTED:key]"),
	(re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), rf"\1{REDACTED}@"),
	(
		re.compile(
			r"(?i)([?&](?:x-amz-signature|x-amz-credential|x-amz-security-token|signature|sig|token|access_token|api_key|key)=)[^&\s\"'#]+"
		),
		rf"\1{REDACTED}",
	),
	(
		re.compile(
			rf"(?i)([\w.-]*(?:{SECRET_NAMES})[\w.-]*[\"']?[ \t]*[:=][ \t]*[\"']?)(?!\[REDACTED)[^\s\"',;}}]+"
		),
		rf"\1{REDACTED}",
	),
)
WORD = re.compile(r"(?<![\w+/=-])[\w+/=-]{20,}(?![\w+/=-])")
UUID = re.compile(r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HEX = re.compile(r"(?i)^[0-9a-f]{32,}$")
SECRET_KEY_NAME = re.compile(rf"(?i){SECRET_NAMES}")
MINIMUM_ENTROPY_BITS = 4.0
MINIMUM_ENTROPY_BITS_WITHOUT_DIGIT = 4.3
MINIMUM_LENGTH_WITHOUT_DIGIT = 32


def scrub(text: str) -> str:
	"""Mask credentials and high-entropy words. IP addresses, host names, and record names stay."""
	for pattern, replacement in SECRET_PATTERNS:
		text = pattern.sub(replacement, text)
	return WORD.sub(_mask_random_word, text)


def scrub_value(value: object) -> object:
	"""Scrub every string in a JSON-like value. A secret-like key hides its whole value."""
	if isinstance(value, str):
		return scrub(value)
	if isinstance(value, dict):
		return {
			key: REDACTED
			if SECRET_KEY_NAME.search(str(key)) and item not in (None, "")
			else scrub_value(item)
			for key, item in value.items()
		}
	if isinstance(value, list | tuple):
		return [scrub_value(item) for item in value]
	return value


def scrub_complete_lines(text: str) -> str:
	"""Scrub the text up to its last newline. The rest may be a secret that is not complete yet."""
	end = text.rfind("\n") + 1
	return scrub(text[:end])


def _mask_random_word(match: re.Match[str]) -> str:
	word = match.group()
	return "[REDACTED:entropy]" if is_random_word(word) else word


def is_random_word(word: str) -> bool:
	"""Return whether a word looks like a key, a token, or a hash rather than a name."""
	if UUID.match(word):
		return False
	if HEX.match(word):
		return True

	has_mixed_case = any(character.isupper() for character in word) and any(
		character.islower() for character in word
	)
	if not has_mixed_case:
		return False

	entropy = _entropy_bits(word)
	if any(character.isdigit() for character in word):
		return entropy >= MINIMUM_ENTROPY_BITS
	return len(word) >= MINIMUM_LENGTH_WITHOUT_DIGIT and entropy >= MINIMUM_ENTROPY_BITS_WITHOUT_DIGIT


def _entropy_bits(word: str) -> float:
	"""Shannon entropy of the characters of word, in bits per character."""
	counts = Counter(word)
	return -sum(count / len(word) * math.log2(count / len(word)) for count in counts.values())
