# Rejected patterns

Patterns agents produced and the team rejected. Do not repeat them.

## Output

| Pattern | Team's words |
|---|---|
| Long summaries, unasked tables | "no verbosity" |
| Explaining why a change was made | "i dont need to know the reason of change, if needed will ask you" |
| Re-proposing a settled spec | "whatever i said do it man" |
| Repeating that tests are missing | "dont tell everytime" |
| Confident wrong claim, then a "fix" | "the cron is valid in frappe please dont do any shit changes" |
| Rabbit holes, wrong layer | "stop it, waste of time", "wait you going in wrong direction" |

## Code

| Pattern | Team's words |
|---|---|
| Comments restating code or the change | "remove useless comments", "i have trimmed comments, dont add back" |
| File-top comments | "please remove comment from top of the file" |
| Comments hinting at old code | "dont add comment which gives old implementation vibe" |
| Magic constants, field-name constants | "dont make constant for doctype field name" |
| Status mapping tables | "lets not use mapping, use the same state" |
| Many tiny private helpers, `helpers.py` | "dont keep adding lot of private functions", "keep those inline instead" |
| Clever save helpers, manual commits | "simple code is much better than clever code" |
| `save_progress()` steps | "the functions can have save: bool = True" |
| `cast()`, relative imports, `#:` | "import the class in type checking block", "use absolute", "use normal python comment" |
| Dense Go | "let it be multi line" |
| Custom Cobra framework | "use cobra's official guide" |
| `generate.go`, checked-in `.o`, cgo, `bpfel` names | "keep simple, instead add in make", "call it just track_traffic.o" |
| Generic `kind/key` route | "lets expand for domains and sites" |
| Abbreviations | "engg should understand what the method does from method name" |
| Defensive code to pass a test | "fix the test then?" |
| Trimming real rationale | "you removed lot of context on why multi attempt needed" |

## Design

| Pattern | Team's words |
|---|---|
| Unneeded knobs and router flags | "is_proxy_docs_auth_required dont add these stuffs", "dont add allow guest please" |
| Hardcoded consumers | "i dont like this hardcoding of cargo" |
| Extra infrastructure for convenience | "why we need the hub, its NxN na?" |
| New address range | "we have already one setup for it at fdab" |
| Userspace daemon for a kernel feature | "i dont want userspace service" |
| VXLAN over WireGuard | "will lose the multicast benefit" |
| Convoluted poll-and-wake reconciler | "one goroutine running to listen for such events" |
| Internal feature as a status | "no sleeping shit kind of status, just stopped" |
| Partial "capacity pending" states | "tell user that out of capacity and retry later" |
| Cache invalidated every second | "its useless. revert it" |
| Wider placement deadline | The signal was wrong, not the timer. |
| `0.0.0.0` binds | "a big no" |
| AAAA records when the proxy routes | "they will point to proxy only" |
| Child table for one field; JSON for readable records | "could be just field?", "update in the doctype with child table only" |
| Storing derivable values or private keys | "that's a promise", "assume that is on disk, dont store in db" |
| Public IP on a host at reservation | "you shouldn't assign the ip to that server during reserving" |

## Docs and UI

| Pattern | Team's words |
|---|---|
| Hard-wrapped Markdown | "keep continuous line" |
| Docs for obvious behavior | "natural expectation" |
| Over-trimmed or rewritten copy | "you made the docs worse", "old one was better. you missing the point" |
| Old-implementation docs, duplication | "current state only", "i hate duplication anywhere" |
| Bad diagrams, README ASCII charts | "very bad quality diagram man" |
| Worse home page, Excalidraw or Remotion decks | "revert old one was better" |
| Prescribed paths | "just tell it to pass the file path" |
| Field descriptions everywhere, CSS hacks | "keep minimal nice", "just give some title for section" |
| Colored cards, useless polish | "please no color in card", "revert it, useless" |

## Workflow

| Pattern | Team's words |
|---|---|
| Unapproved commit or push | "before commit ask me from next time", "dont push man" |
| `git stash` on the team's tree | "hae you stashed my changes??" |
| Dev servers or browser automation in the frontend repo | "dont do it" |
| Tests on the real dev site | "never run unit test on atlas.localhost" |
| Committed plan files, wrong scope, odd messages | "use only atlas as scope", "dont use the word clarify" |
| Long PR bodies, literal `\n` | "keep it smaller and compact man" |
| Editing incident reports | "why you modifying past reports" |
| Changing another repo's flow | "fix it in atlas's install-cargo.sh" |
