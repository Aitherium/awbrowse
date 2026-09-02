# awbrowse

<!-- aither-header:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

**[Docs](https://aitherium.github.io/awbrowse/)**  ·  [Source](https://github.com/Aitherium/awbrowse)  ·  `pip install awbrowse`  ·  [The Aither World](https://aitherium.github.io/)

> **The Aither World** is an operating system for agents — a Linux you can hand to one, the runtimes it works in, and the tools it works with. [awnix](https://github.com/Aitherium/awnix) is the Linux underneath it; **awbrowse** is one of its 41 bricks — each installs on its own, runs offline, and needs no account.
>
> **Start here:** Drive one page and get back what is on it in a form worth reasoning about.

<!-- aither-header:end -->

**A real browser, from an agent, over a service you host.**

```bash
pip install awbrowse
```

```python
from awbrowse import BrowseClient

b = BrowseClient("https://browser.example.com", token="...")
page = b.browse("https://example.com")
print(page.ok, page.engine, len(page.text))
```

```bash
awbrowse get https://example.com     # render, print the text
awbrowse shot https://example.com    # -> screenshot.jpg (extension sniffed)
awbrowse --self-test                 # prove the contract, offline
```

The service origin comes from `--url` or `AWBROWSE_URL`, the token from `--token`
or `AWBROWSE_TOKEN`. Neither is guessed — a client that quietly falls back to
some default endpoint is sending your pages somewhere you did not choose.

---

## What this is, and what it is not

It is a **client**. The rendering engine — headless browsers, session pools,
scrape schemas — stays where it is; this is the wire contract, packaged so
anything can speak it. Point it at any service exposing the routes below.

That split is deliberate. The alternative was lifting a 1,200-line service with
a dozen private imports into a package, which produces something that
`ModuleNotFoundError`s on your machine while reading as authoritative. A broken
package is worse than no package.

| route | what it does |
|---|---|
| `POST /browse` | render one page |
| `POST /scrape` | structured extraction |
| `POST /session/open` | a persistent, authenticated session |
| `POST /session/{id}/act` | click, type, navigate |
| `POST /session/{id}/observe` | read the page back |
| `GET /session/{id}/network` | what the page actually requested |

---

## Authenticated pages go through a session

`browse()` cannot take cookies, and that is a feature.

The service's browse model declares `extra="forbid"`. It did not always: with
pydantic's default `extra="ignore"`, a request carrying `cookies` or
`storage_state` had those fields **silently dropped**, so an "authenticated"
render ran anonymous and returned the login page as though it were the app —
HTTP 200, correct-looking output, completely wrong answer.

So this client sends exactly the four declared fields and nothing else, and
credentials go where the server actually reads them:

```python
with b.open_session(storage_state=...) as s:
    s.fill("#user", "me")                 # value= , not text=
    s.click("#login")
    o = s.observe()                       # url, title, text, ELEMENTS, screenshot
    for el in o.elements:                 # what an agent decides to click from
        print(el["tag"], el["text"], el["selector"])
    for r in s.network(clear=False):      # why a page that "renders fine" is failing
        print(r["status"], r["content_type"], r["url"])
```

`session.network()` is the reason to reach for a session while debugging: a page
that renders correctly and fails silently is usually failing in a request nobody
looked at. **Two things make an empty list mean nothing**, and both are measured:
it is FILTERED (only JSON-ish responses — content-type json, `/api/` in the URL,
or `.json`; never a 4xx/5xx and never a preflight), and the default DRAINS the
buffer server-side, so a second consecutive call returns nothing.

`observe()` is a **different shape** from `browse()` — it has a `title` and an
`elements` list that `/browse` does not send. They are separate types for that
reason: parsed as a `Page`, `elements` vanishes and an agent picks what to click
from a list it never saw.

An action the service does not dispatch is refused here, because it would not be
refused there: the server's dispatch is an `if/elif` chain with no `else`, so a
typo'd action is a **no-op that answers 200** — "I clicked and nothing happened".
Same for a misspelt `open_session` field: unlike `/browse`, that model does *not*
forbid extras, so a mistyped `storage_state` is dropped in silence and hands you
an anonymous session that looks logged in.

---

## What a live probe found that no offline test could

Both of these shipped in the first draft, and both are the failure this package
exists to refuse — a request that is valid, a response that is **200**, and a
key that is simply absent:

**The screenshot key is not the screenshot field.** The request says
`screenshot: true`; the response says **`screenshot_base64`**. Reading the
request's name back off the response yields `None` on every successful capture,
so `awbrowse shot` would have reported *"the service returned no screenshot"*
forever while the service sent one every time.

**The capture is JPEG, not PNG.** Nothing in the request says so. A default
output of `screenshot.png` writes JPEG bytes under a name that lies about them —
fine in a viewer that sniffs, broken in anything that trusts the extension. So
the extension is sniffed from the magic bytes, and an `-o` whose extension
disagrees is honoured *and* mentioned.

`Page` therefore exposes exactly what the service was measured to send —
`status`, `url`, `engine`, `content`, and `screenshot_base64` when asked. No
`title`, no `html`: this route sends neither, and an attribute that is empty on
every response is worse than an absent one, because callers branch on it.
Everything else is on `.raw`.

---

## Two things it refuses to do

**Return an empty page on failure.** A dead service raises `BrowseError`. If it
returned `""`, a transport failure and a genuinely blank page would be the same
value to every caller, and the outage would look like a boring page.

**Send an empty `Authorization` header.** No token means no header at all. An
empty Bearer is rejected differently from an absent one, and the difference sends
you debugging the auth server instead of your config.

---

## `--self-test`

Every install can prove the client still holds its contract, with no service and
no network:

```console
$ awbrowse --self-test
  PASS  browse body is exactly the declared field set
  PASS  text reads `content`; screenshot reads the MEASURED response key
  PASS  a failed render raises rather than arriving as a blank page
  PASS  network reads the MEASURED key; an undispatched action is refused
  PASS  image format sniffed, not assumed; base_url normalised; no empty Bearer
SELF-TEST: awbrowse ok
```

It asserts the **exact** request field set rather than membership — an extra key
is a 422 for the whole request, and a test that only checks "url is in there"
passes on that bug.

---

<!-- aither-ecosystem:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

## The aw family

Standalone tools that share one idea: **replace something you would otherwise have to _trust_ with something you can _check_.**

Each installs on its own, works offline, and needs no account.

| | instead of trusting | you check |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | a framework's idea of how your agents should run | one loop you can read, pointed at a backend you already pay for |
| [awskills](https://github.com/Aitherium/awskills) | that an agent knows your procedure | the procedure written down, versioned, and loadable by any agent |
| [awpack](https://github.com/Aitherium/awpack) | that the pack you want shipped inside somebody's SDK, under whatever licence that SDK happens to carry | the pack as its own versioned artifact, with its own licence, that any agent runtime can install |
| [awm](https://github.com/Aitherium/awm) | that memory stayed in its lane | tenant:user:project scopes, so a write cannot cross a boundary |
| [awnode](https://github.com/Aitherium/awnode) | a vendor's cloud with every prompt | a local gateway routing to backends you chose |
| [awgraph](https://github.com/Aitherium/awgraph) | that grep found everything | an AST + tree-sitter call graph an agent can traverse |
| [awgit](https://github.com/Aitherium/awgit) | that no one else is editing this file | a lease, refused at commit time if you do not hold it |
| [awdelphi](https://github.com/Aitherium/awdelphi) | one agent's confident take on a decision | the round trace, the anonymity, and who dissents |
| [awtoll](https://github.com/Aitherium/awtoll) | that your tooling is saving you context | the measured token cost of each tool call, and what the alternative cost |
| [awseal](https://github.com/Aitherium/awseal) | that the artifact came from who you think | an Ed25519 seal — the key that verifies is not the key that forges |
| [awshare](https://github.com/Aitherium/awshare) | that the download is intact | content-addressed bundles, verified on fetch |
| [awnest](https://github.com/Aitherium/awnest) | that there is a person on the other end | a verdict with evidence, where "we could not tell" is not "yes" |
| [awnboard](https://github.com/Aitherium/awnboard) | a share link anyone who sees it can use | an invitation addressed to one person, for one gate, revocable |
| [awnix](https://github.com/Aitherium/awnix) | that the box is what you left it as | an immutable image you built, with atomic rollback |
| [awrecover](https://github.com/Aitherium/awrecover) | that the restore worked | a restore that fully lands or does not land at all |
| [awrelay](https://github.com/Aitherium/awrelay) | a SaaS in the middle of your agents | findings, alerts and coordination over your own transport |
| [awmail](https://github.com/Aitherium/awmail) | a mailbox somebody else can read | mail your agents send and receive over your own server |
| [awfind](https://github.com/Aitherium/awfind) | one vendor's idea of the web | results from whichever providers you configured |
| **awbrowse** _(you are here)_ | that the page said what you were told | the render, the DOM and the requests it made |
| [gobbonet-agentic](https://github.com/Aitherium/gobbonet-agentic) | the model to keep a 300-message campaign coherent by itself | campaign facts recalled from scoped memory you can list and edit |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | a vendor's quantisation defaults | sub-byte KV cache kernels you can benchmark yourself |
| [awrtifact](https://github.com/Aitherium/awrtifact) | a hand-rolled split script and a hand-edited worker manifest | byte-verified parts in a release, served with Range + CORS, sizes asserted by a live gate |
| [AitherZero](https://github.com/Aitherium/AitherZero) | a pile of scripts nobody has numbered | numbered, discoverable automation with declarative playbooks |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | what a page tells your browser to do | a federated search and desktop bridge you host |
| [awreason](https://github.com/Aitherium/awreason) | a confident paragraph | the phases it went through, and every tool call it made to get there |
| [awrecurse](https://github.com/Aitherium/awrecurse) | that everything you pasted in was actually read | which slices it opened, and what it concluded from each |
| [awprism](https://github.com/Aitherium/awprism) | the first explanation that fits | the ranked alternatives, and the observation that separates them |
| [awrepl](https://github.com/Aitherium/awrepl) | what the agent believes the value is | the value, printed from the live session |
| [awresearch](https://github.com/Aitherium/awresearch) | a summary of pages nobody opened | every claim against the source it came from |
| [awfocus](https://github.com/Aitherium/awfocus) | twelve terminal tabs and a bad memory | one command that names every session, finds any transcript, and opens or steers the one you want |
| [awgym](https://github.com/Aitherium/awgym) | that a world model learned anything from the games it saw | transitions captured from real play, fed back, and the retrodiction score falling on grids it never saw |
| [awpredict](https://github.com/Aitherium/awpredict) | a model because it trained without erroring | its prediction against a self-updating lookup, on the rows that are actually novel |
| [awsh](https://github.com/Aitherium/awsh) | that you already know the name of the command | what it decided your line meant, before it acts on it |
| [awkno](https://github.com/Aitherium/awkno) | that the docs site is up, or that you remember the family | the whole ecosystem in your terminal, with no network at all |

[**awnix**](https://github.com/Aitherium/awnix) is the ground floor — A Linux you can hand to an agent — immutable base, capabilities included.

## The Aitherium ecosystem

Every repository here is public. Each publishes an `aither-manifest.json` beside its page, so any surface can read every sibling's — the network is browsable from any node in it.

| repo | what it is | pages |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | Build AI agent fleets — 3 lines, any backend, local or cloud | [docs](https://aitherium.github.io/awdk/) |
| [awskills](https://github.com/Aitherium/awskills) | Portable agent skills — self-contained procedures an agent loads on demand | [docs](https://aitherium.github.io/awskills/) |
| [awpack](https://github.com/Aitherium/awpack) | First-party agent packs — the ones we build, versioned and installable on their own | [docs](https://aitherium.github.io/awpack/) |
| [awm](https://github.com/Aitherium/awm) | A portable, scoped agent memory | [docs](https://aitherium.github.io/awm/) |
| [awnode](https://github.com/Aitherium/awnode) | A lightweight local gateway — bridges your apps to the AI backends you chose | [docs](https://aitherium.github.io/awnode/) |
| [awrun](https://github.com/Aitherium/awrun) | A priority-aware queue and dispatcher for agentic runs and ad-hoc CI builds. It also judges whether the runner pool is big enough for the queue it is draining, and can ask a host to grow it -- reserving capacity is zero-sum, so a saturated pool needs more of it, not a different share of it | [docs](https://aitherium.github.io/awrun/) |
| [awgraph](https://github.com/Aitherium/awgraph) | A semantic code graph for agents — AST + tree-sitter, call graphs | [docs](https://aitherium.github.io/awgraph/) |
| [awgit](https://github.com/Aitherium/awgit) | Semantic version control on top of git — edit-ops and leases | [docs](https://aitherium.github.io/awgit/) |
| [awdelphi](https://github.com/Aitherium/awdelphi) | Anonymous multi-round expert panels — a converged answer with a trace | [docs](https://aitherium.github.io/awdelphi/) |
| [awtoll](https://github.com/Aitherium/awtoll) | What every tool call costs you in context, measured from your own transcripts | [docs](https://aitherium.github.io/awtoll/) |
| [awseal](https://github.com/Aitherium/awseal) | Sign an artifact so a stranger can verify it | [docs](https://aitherium.github.io/awseal/) |
| [awshare](https://github.com/Aitherium/awshare) | Publish an artifact and fetch it back verified | [docs](https://aitherium.github.io/awshare/) |
| [awdit](https://github.com/Aitherium/awdit) | An append-only audit trail whose gaps are DETECTABLE | [docs](https://aitherium.github.io/awdit/) |
| [awbac](https://github.com/Aitherium/awbac) | Role-based access control that fails closed and explains itself | [docs](https://aitherium.github.io/awbac/) |
| [awiam](https://github.com/Aitherium/awiam) | Who is this caller? A directory and session store that fails honestly | [docs](https://aitherium.github.io/awiam/) |
| [awtunnel](https://github.com/Aitherium/awtunnel) | Reach a service that has no public address | [docs](https://aitherium.github.io/awtunnel/) |
| [awnest](https://github.com/Aitherium/awnest) | Prove there is a human before you let them into the nest | [docs](https://aitherium.github.io/awnest/) |
| [awnboard](https://github.com/Aitherium/awnboard) | A front gate you can put in front of anything, and hand someone the key to | [docs](https://aitherium.github.io/awnboard/) |
| [awnix](https://github.com/Aitherium/awnix) | A Linux you can hand to an agent — immutable base, capabilities included | [docs](https://aitherium.github.io/awnix/) |
| [awrecover](https://github.com/Aitherium/awrecover) | Labelled snapshots with an all-or-nothing restore | [docs](https://aitherium.github.io/awrecover/) |
| [awrelay](https://github.com/Aitherium/awrelay) | Portable agent messaging — findings, alerts, coordination | [docs](https://aitherium.github.io/awrelay/) |
| [awmail](https://github.com/Aitherium/awmail) | Give an agent an email address — send, and actually receive | [docs](https://aitherium.github.io/awmail/) |
| [awnet](https://github.com/Aitherium/awnet) | The agentic web — agents host a mesh, and agents join one | [docs](https://aitherium.github.io/awnet/) |
| [awfind](https://github.com/Aitherium/awfind) | A portable search client — query, results, ranking | [docs](https://aitherium.github.io/awfind/) |
| **awbrowse** _(you are here)_ | A portable browser client — navigate, console, network, DOM, screenshot | [docs](https://aitherium.github.io/awbrowse/) |
| [awknowledge](https://github.com/Aitherium/awknowledge) | How to run a coding agent so the result survives — the laws, with evidence | [docs](https://aitherium.github.io/awknowledge/) |
| [gobbonet-agentic](https://github.com/Aitherium/gobbonet-agentic) | GobboNet campaigns with a real agent brain — scoped memory, graph recall | [docs](https://aitherium.github.io/gobbonet-agentic/) |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | Near-optimal KV cache quantization for LLM inference — sub-byte compression | [docs](https://aitherium.github.io/aitherkvcache/) |
| [awrtifact](https://github.com/Aitherium/awrtifact) | Deliberately chunk artifacts into GitHub release assets — the productized aitherkvcache mirror lane | [docs](https://aitherium.github.io/awrtifact/) |
| [AitherZero](https://github.com/Aitherium/AitherZero) | PowerShell 7+ automation framework — numbered, self-describing scripts | [docs](https://aitherium.github.io/AitherZero/) |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | Browser extension — federated AI search, page context, and the Living OS overlay | [docs](https://aitherium.github.io/AitherConnect/) |
| [awreason](https://github.com/Aitherium/awreason) | A portable reasoning client — sessions, phases, thoughts, and the chain that produced the answer | [docs](https://aitherium.github.io/awreason/) |
| [awrecurse](https://github.com/Aitherium/awrecurse) | Answer a question over a context far larger than the window — recursively, with the trace kept | [docs](https://aitherium.github.io/awrecurse/) |
| [awprism](https://github.com/Aitherium/awprism) | Turn a failure into ranked hypotheses — and say what would confirm each one | [docs](https://aitherium.github.io/awprism/) |
| [awrepl](https://github.com/Aitherium/awrepl) | A REPL an agent can actually use — state that survives between turns | [docs](https://aitherium.github.io/awrepl/) |
| [awresearch](https://github.com/Aitherium/awresearch) | Ask a research question, get a cited report you can check | [docs](https://aitherium.github.io/awresearch/) |
| [awfocus](https://github.com/Aitherium/awfocus) | See, search and steer every Claude session from one command | [docs](https://aitherium.github.io/awfocus/) |
| [awgym](https://github.com/Aitherium/awgym) | An ARC training gym — a game a world model can watch, and six roles that play through it | [docs](https://aitherium.github.io/awgym/) |
| [awpredict](https://github.com/Aitherium/awpredict) | Predict what your environment does next, and how surprised you were | [docs](https://aitherium.github.io/awpredict/) |
| [awsh](https://github.com/Aitherium/awsh) | Your terminal answers you -- type a question where a command would go | [docs](https://aitherium.github.io/awsh/) |
| [awkno](https://github.com/Aitherium/awkno) | The man page for the Aither World — every brick, stack and law, offline | [docs](https://aitherium.github.io/awkno/) |

<div id="aither-constellation" data-self="awbrowse"></div>
<script src="aither-constellation.js"></script>

<!-- aither-ecosystem:end -->
