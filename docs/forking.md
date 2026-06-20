# Forking and folding in pvl-core

`fastmcp-pvl-core` is the shared core of the `pvliesdonk/*-mcp` server family.
It is MIT-licensed. If you want to take over a single server when the upstream
is no longer maintained, or run your own opinionated variant, you can **fold
pvl-core into your fork** and cut the upstream dependency entirely. A fork is
not a downstream — none of the family's coherence rules bind you once you fold.

## First decide: pin, or fold?

**Pin and forget.** Keep `fastmcp-pvl-core==X.Y.Z` pinned and stop running
`copier update`. Zero effort. You keep receiving nothing new — including no
dependency or CVE bumps — and you cannot modify the core. Best if you are happy
with the core as-is and only want to freeze it.

**Fold in (vendor).** Copy the package into your tree, rename it, drop the
dependency. You get full ownership and the freedom to modify, at the cost of
owning the full maintenance burden — including tracking transitive CVEs that
upstream used to handle for you. Choose this only if you actually intend to
change the core or cannot rely on upstream at all.

The rest of this guide covers folding in.

## Fold-in recipe

pvl-core uses relative intra-package imports, so folding is a directory rename
— you do not edit the core's internal imports.

```bash
# 1. Copy the package into your fork (rename to your own internal package):
cp -r path/to/fastmcp_pvl_core  src/myfork/_core

# 2. Update YOUR code's imports from the dependency to the vendored package:
#    from fastmcp_pvl_core import wire_middleware_stack   ->   from myfork._core import wire_middleware_stack
grep -rl 'fastmcp_pvl_core' src/myfork --include='*.py'   # find your call sites
#    ...then rewrite those `from fastmcp_pvl_core` references to `from myfork._core`.

# 3. Drop the dependency from pyproject.toml (remove the fastmcp-pvl-core line).

# 4. Reinstall and run your tests.
```

## Bring the tests

Vendor the test suite too — it is your safety net for the flattening below:

```bash
cp -r path/to/tests  tests/_core
# Rewrite the suite's absolute imports to your vendored package name:
#   from fastmcp_pvl_core import X   ->   from myfork._core import X
```

## Collapsible-seams map

pvl-core carries abstractions because it serves *five* servers. Your fork serves
one, so you may collapse them. These are safe to flatten **in a fork** — do not
ask pvl-core to pre-flatten them, that would break the family:

- **`env(prefix, name)` indirection** — pvl-core parameterizes the env-var
  prefix so each server picks its own. Your fork has one prefix; inline it.
- **`Build*` / factory layer** — the factory exists to assemble a server from
  config generically. With one server you can inline the construction at its
  single call site.
- **Parameterized CLI `prog`** — `make_serve_parser(prog=...)` lets each server
  name its own program. Hard-code your fork's name.
- **Optional-dependency extras** — pvl-core splits backends (`[redis]`,
  `[dynamodb]`, `[mongodb]`, `[remote-auth]`, `[debug]`) behind extras. Keep
  only the backends your fork uses and make them hard dependencies.

## Cosmetic scrub list

These reference pvl-core by name but are harmless until you want the fork to
look fully its own. Search-and-replace at your leisure:

- The version label in `_server_info.py` (reports `fastmcp-pvl-core` + version).
- The `pip install fastmcp-pvl-core[...]` hints in `_debug.py`, `_auth.py`,
  `_kv_store.py` (shown when an optional extra is missing).
- The `"file an issue against fastmcp-pvl-core"` pointer in the
  FastMCP-internal-API `RuntimeError` in `_icons.py`.
- The `from fastmcp_pvl_core import SecretMaskFilter` usage example in the
  `SecretMaskFilter` docstring in `_logging.py` — a downstream-facing import
  path; point it at your vendored package name.
- Sphinx-style docstring cross-references (`:class:`~fastmcp_pvl_core....``) and
  the `fastmcp_pvl_core_current_auth_mode` ContextVar name, if you rename the
  package for real.

None of these are functional couplings — pvl-core performs no runtime lookup of
its own distribution name or package resources, so renaming never breaks
imports or resource loading.
