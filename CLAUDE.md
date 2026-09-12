# ParadiseBlenderEditor agent guide

One Blender extension, `paradise_assets`, edits the game repository's canonical `assets/*.prefab`
documents. The `.blend` under `.editor/blend/` is a disposable view of one document. The
`paradise` CLI compiles assets into `build/` for the game.

The former `paradise_blender` exporter and its .NET bridge were removed. Notes about exporting
to `data/`, Python KTX conversion, name-derived identity or automatic model-prefab mirroring
are historical; they do not describe this addon.

Complete the requested behavior and relevant validation, including fixing failures introduced
by the change. If the task includes running Blender or checking the resulting document, carry
it through that verification and report any concrete blocker.

## Task-specific references

- Transforms, float formatting, identity or names: [CONVENTIONS.md](CONVENTIONS.md).
- Panels, menus or project/document scope: [addon layout and UI](docs/addon-ui.md).
- Registration and handlers: [addon lifecycle](docs/document-contracts.md#addon-lifecycle).
- Document writers and byte parity: [canonical serialization](docs/document-contracts.md#canonical-serialization).
- Load, reload, grouping or collision shapes: [session and hierarchy editing](docs/document-contracts.md#session-and-hierarchy-editing).
- Save, component overlays or prefab child changes: [prefab overrides and save state](docs/document-contracts.md#prefab-overrides-and-save-state).
- Component schemas, sidecars or prefab creation: [schema, identity and extraction](docs/document-contracts.md#schema-identity-and-extraction).
- Cross-language format changes: [format update procedure](docs/document-contracts.md#when-the-document-format-changes).

## Core boundaries

- Read component payloads from the current document; apply only the fields the author edited.
  Unknown components and untouched values survive a save.
- The CLI watcher alone mints asset sidecars. Generated model prefabs become ordinary authored
  documents after creation; do not recreate a mirror that overwrites or deletes them.
- Keep `document/`, `edits.py`, and module-level `paradise_assets/__init__.py` free of `bpy` imports.
- A panel draw must not log, walk the asset tree or write ID properties; use the existing caches
  and explicit synchronization paths.

## Commands

Choose unit or Blender integration checks for the changed behavior. Integration tests accept
`PARADISE_ASSETS_PROJECT` and skip project-dependent checks when no project is available.

```bash
.venv/bin/python -m pytest tests/unit -q
blender --background --factory-startup --python tests/integration/test_axis_parity.py
./tools/run_tests.sh
python3 tools/install_addon.py                 # symlink into Blender's extensions dir
```

## Style and Git

Match the Python typing conventions, including `from __future__ import annotations`. Comments
explain Blender API constraints, data contracts and decisions. Warnings should tell the author
what will go wrong at runtime and how to fix it.

This is an independent repository. Keep each commit within it and use a separate worktree for
each implementation task. Commit and push only when requested or already authorized by the task.
Assign PRs to quabug. Issue fixes put `Closes #NNN` at the top of the PR body and in the commit
message, one line per issue; use `Towards #NNN` only for deliberately partial work.
