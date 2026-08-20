# awbrowse

**A real browser, from an agent, over a service you host.**

```bash
pip install awbrowse
```

```python
from awbrowse import BrowseClient

b = BrowseClient("https://browser.example.com", token="...")
page = b.browse("https://example.com")
print(page.title, len(page.text))
```

```bash
awbrowse get https://example.com          # render, print the text
awbrowse shot https://example.com -o p.png
awbrowse --self-test                      # prove the contract, offline
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
    s.act("click", selector="#login")
    page = s.observe()
    for req in s.network():          # why a page that "renders fine" is failing
        print(req["status"], req["url"])
```

`session.network()` is the reason to reach for a session while debugging: a page
that renders correctly and fails silently is usually failing in a request nobody
looked at.

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
  PASS  Page text fallback, and absent screenshot is None
  PASS  base_url normalised; no token means no header
SELF-TEST: awbrowse ok
```

It asserts the **exact** request field set rather than membership — an extra key
is a 422 for the whole request, and a test that only checks "url is in there"
passes on that bug.

---

## The aw family

Standalone tools that share one idea: **replace something you would otherwise
have to _trust_ with something you can _check_.** Each installs on its own, works
offline, and needs no account.

| | instead of trusting | you check |
|---|---|---|
| **awbrowse** _(you are here)_ | that the page said what you were told | the render, the DOM and the requests it made |
| [awfind](https://github.com/Aitherium/awfind) | one vendor's idea of the web | results from whichever providers you configured |
| [awnix](https://github.com/Aitherium/awnix) | that the box is what you left it as | an immutable image you built, with atomic rollback |
| [awgit](https://github.com/Aitherium/awgit) | that no one else is editing this file | a lease, refused at commit time if you do not hold it |
| [awgraph](https://github.com/Aitherium/awgraph) | that grep found everything | an AST + tree-sitter call graph an agent can traverse |
| [awrelay](https://github.com/Aitherium/awrelay) | a SaaS in the middle of your agents | findings and alerts over your own transport |
| [awseal](https://github.com/Aitherium/awseal) | that the artifact came from who you think | an Ed25519 seal — the key that verifies is not the key that forges |
| [awshare](https://github.com/Aitherium/awshare) | that the download is intact | content-addressed bundles, verified on fetch |
| [awm](https://github.com/Aitherium/awm) | that memory stayed in its lane | tenant:user:project scopes |
| [awrecover](https://github.com/Aitherium/awrecover) | that the restore worked | a restore that fully lands or does not land at all |

Apache-2.0.
