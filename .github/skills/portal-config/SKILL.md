---
name: portal-config
description: 'Update job portal selectors and config in this repo. Use for portal YAML changes, adapter issues, selector bugs, and config-driven portal maintenance.'
argument-hint: 'portal name, selector issue, or config change'
user-invocable: true
disable-model-invocation: false
---

# Portal Configuration Workflow

## When to use

Use this skill when a portal fails because of:

- a selector or DOM path changed on a portal
- a config value in `config/portals/*.yaml` is wrong or outdated
- an adapter needs a small site-specific adjustment without hardcoded CSS in Python
- a portal is being added, fixed, or validated after a site redesign

This repo intentionally keeps selectors in YAML, not Python. The first instinct for any portal breakage is to inspect the config and YAML-driven adapter path, not to patch a Python scraper directly.

## Core principles

- Keep selectors in `config/portals/*.yaml`; do not hardcode CSS selectors in Python.
- Prefer a config fix over a code rewrite when the adaptor is still `ConfigDrivenAdapter`-based.
- Preserve the repo safety invariant: the app stops before submitting automatically and never bypasses the human review gate.
- Validate config and portal resolution before claiming the fix is complete.

## Procedure

1. Identify the affected portal and adapter.
   - Check the portal entry in `config/portals/*.yaml`.
   - Confirm the `adapter:` string resolves via the registry in `src/jobauto/portals/registry.py`.
   - Read the corresponding portal adapter in `src/jobauto/portals/` if the site differs materially from the generic config-driven behavior.

2. Inspect the YAML-driven selectors and behavior.
   - Look for fields like selectors, actions, page patterns, and pagination config.
   - Compare them to the generic portal pattern used by the other adapters.
   - Prefer a minimal YAML edit if the issue is a broken CSS selector or a changed DOM layout.

3. Decide whether the fix is config-only or requires a small adapter override.
   - If the site still follows the standard flow, update the YAML selectors and keep Python untouched.
   - Only override a Python adapter when the portal genuinely differs (chatbot drawer, iframe, feed search, Easy Apply wizard, etc.).

4. Update the relevant config and keep it consistent with the template patterns.
   - Follow the same key names and semantics used by existing portal config files.
   - Do not introduce ad hoc selectors or one-off logic scattered across the codebase.

5. Validate the change with the smallest relevant checks.
   - Run the portal/config tests that exercise YAML validity and adapter resolution.
   - If needed, run a focused pytest subset for config and pipeline behavior.
   - If this touches a page interaction that is not covered by tests, explain the risk and prefer the smallest verification possible.

## Completion checks

Before finishing, confirm all of the following:

- The selector or config change matches the current portal structure.
- No CSS selector was added to a Python file that should live in YAML.
- The portal still resolves via the registry and the config is valid.
- The fix does not weaken the product’s manual-review invariant.
- Relevant tests pass or the user is informed of the missing verification.

## Useful references

- [README.md](../../../README.md)
- [CLAUDE.md](../../../CLAUDE.md)
- [src/jobauto/portals/registry.py](../../../src/jobauto/portals/registry.py)
- [src/jobauto/portals/base.py](../../../src/jobauto/portals/base.py)
- [src/jobauto/config.py](../../../src/jobauto/config.py)
- [config/](../../../config/)

## Example prompts

- "Fix the Naukri selector for the application button."
- "A portal config changed and the adapter no longer resolves. What should I update?"
- "The LinkedIn job card selectors are stale; update the YAML without hardcoding CSS in Python."
- "Validate this portal config change against the repo conventions and run the smallest useful tests."

## Related customizations

- A repo-level instruction file for project conventions and safety invariants.
- A test-focused prompt for validating config and portal paths with the smallest relevant pytest run.
