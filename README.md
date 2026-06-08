# MCodePatcher for MCreator

MCodePatcher helps you customize generated MCreator code without manually reapplying the same edits every time a workspace regenerates.

It keeps your custom changes in a separate patch tree, grouped by workspace, generator, and Minecraft version. While MCodePatcher is running, it watches your generated workspace and reapplies matching customizations when eligible files change.

You can use it to:

- Replace entire generated files with file overrides
- Insert reusable code snippets using markers
- Replace generated headers/import sections
- Add imports cleanly to Java import blocks
- Remove generated code regions
- Apply exact find/replace patches
- Apply advanced literal or regex-based rules across targeted files or directories
- Track stale, missing, modified, and out-of-sync patch relationships
- Restore pre-patch backups when needed

MCreator generates the workspace. MCodePatcher maintains customizations across regeneration cycles.

---

## Why Not Just Edit Generated Files?

Editing generated files directly works until MCreator regenerates them.

Traditional approaches usually force you to choose between:

- Losing changes during regeneration
- Maintaining a separate fork of generated files
- Manually reapplying edits after updates
- Replacing entire files when only a few lines changed

MCodePatcher provides several levels of customization instead:

| Method | Best For |
|----------|----------|
| File Override | Replacing an entire generated file |
| Snippet Injection | Adding reusable code blocks |
| Header Injection | Replacing generated headers or declarations |
| MIMPORT | Adding imports without manually modifying import sections |
| MREMOVE | Removing generated code regions |
| replacements.json | Small exact text replacements |
| Advanced Rules | Large-scale literal or regex-based transformations |

This allows patches to be as small or as large as needed, reducing the amount of generated code that must be fully overridden and maintained.

---

## Download/Installation

You can download the latest automated builds from GitHub Actions:

1. Go to the **Actions** tab.
2. Select **Build MCodePatcher**.
3. Open the latest successful workflow run.
4. Scroll to **Artifacts**.
5. Download the zip for your OS - it will contain an executable software file you can add to your applications. These are unsigned, so will likely give security warnings upon first opening.
   - `MCodePatcher-Windows`
   - `MCodePatcher-Linux`
   - `MCodePatcher-macOS`

Artifacts are generated automatically by GitHub’s build runners. They may expire after a limited retention period, so use the latest successful run when possible.

---

## How It Works

MCodePatcher maintains a patch tree beside your generated workspace:

```text
MCreator Workspace
├─ src/main/java/...
└─ generated files

MCodePatcher Patch Tree
├─ file overrides
├─ snippet_injections/
├─ header_injections/
├─ advanced_snippet_injections/
└─ replacements.json
```

When MCreator regenerates code:

1. MCodePatcher detects eligible file changes.
2. Matching file overrides, markers, replacements, and advanced rules are located.
3. The patch pipeline applies those changes to the workspace file.
4. Reference and rule history are updated where applicable.
5. A backup is saved before patch writes or restores.

Patch folders are separated by detected generator and Minecraft version, such as forge_1201 or neoforge_1211, allowing different ports to maintain independent patch sets.

---

## Marker-Based Injections

MCodePatcher can patch generated files using markers placed inside MCreator custom code sections.

Example:

java // MSNIPPET:custom_logic 

When patching runs, MCodePatcher looks for:

text snippet_injections/custom_logic.txt 

and inserts that file's contents at the marker location.

With Reference Tracking enabled, the marker is converted into a tracked block:

java // MSNIPPET_APPLIED:custom_logic ...inserted snippet code... // MSNIPPET_END:custom_logic 

These tracking markers allow MCodePatcher to locate and refresh injected content later if the source snippet changes.

Supported marker systems include:

- MSNIPPET for reusable code insertion
- MHEADER for replacing everything above a marker
- MIMPORT for moving imports into the Java import block
- MREMOVE_START / MREMOVE_END for deleting generated code regions

---

## Reference Tracking Off / Clean Export Mode

Reference Tracking can be disabled when you want cleaner exported source files without MCodePatcher tracking comments.

Examples include:

- MSNIPPET_APPLIED
- MSNIPPET_END
- MHEADER_APPLIED
- MIMPORT_APPLIED
- MREMOVE_APPLIED

When tracking is disabled:

- Raw markers continue to function normally.
- Applied tracking markers are cleaned from exported files.
- Existing tracked regions are queued for cleanup.

### Tradeoffs

While tracking is disabled:

- Snippet and Header reference tabs are unavailable.
- Applied markers are removed from patched workspace files.
- Files that become marker-free can no longer be reliably linked back to their originating snippets or headers.
- Updating snippets or headers may not automatically refresh previously cleaned files.
- Missing and valid marker highlighting becomes less useful.
- Import and removal operations become harder to audit after cleanup.

For this reason, Reference Tracking Off is best treated as a final export mode rather than a primary development mode.

File overrides and advanced rule history remain fully functional.

---

## Designed for Regenerating Code

MCreator-generated files change frequently. MCodePatcher is built around that reality.

It tracks:

- Workspace roots
- Generator and Minecraft version folders
- Patch files and workspace counterparts
- Snippet and header relationships
- Advanced rule history
- Backup snapshots
- Out-of-sync file overrides
- Missing, stale, modified, disabled, and unused patch states

As a workspace evolves, MCodePatcher helps identify which customizations are still active, which require attention, and which can be safely carried forward into another generator or Minecraft version.

---

## Workspace Version Manager

Open from:

Workspace → Manage Workspace Versions

Features:

- Link different generator/version folders to different MCreator exports.
- Switch active linked roots within a saved logical workspace.
- Repoint version folders when ports use different workspace directories.
- Merge or copy patch files between generator folders.
- Preserve existing target patch files unless overwrite is enabled.

MCodePatcher automatically generates version-specific patch folder structures when compatible MCreator workspaces are detected.

---

## Workspace Tree

The Workspace Tree allows you to:

- Browse files
- Search files
- Open files
- Reveal files in your operating system

### Create File Override

Creates a patch-tree copy of the selected workspace file.

Workspace edits can trigger:

- File overrides
- Snippet injections
- Header injections
- Advanced rules

---

## Patch Tree

The Patch Tree mirrors the workspace folder structure.

### Organization

Patches are grouped by:

- Workspace
- Generator
- Minecraft version

### File Overrides

Normal files within the patch tree act as file overrides and mirror their workspace-relative locations.

### Disabling Patches

Rename files or folders with:

text .mpatch_disabled 

to disable them.

Multiple files can be selected and enabled or disabled together.

---

## Advanced Rules

Stored as:

text advanced_snippet_injections/name.inject.json 

### Scope Modes

- File
- Directory

Paths are workspace-relative.

### Matching

Supports:

- Literal text matching
- Regular expressions
- Regex flags such as IGNORECASE

### Actions

- replace_first
- replace_all

### Replacements

Can use:

- Inline replacement text
- External snippet file content

### History Status

Rules may report:

- VALID
- STALE
- RULE CHANGED
- MISSING

---

## replacements.json

Provides exact find-and-replace operations for a single workspace-relative file.

Example:

json {   "enabled": true,   "find": "text",   "replace": "replacement",   "replace_mode": "first" } 

Useful for small targeted edits without requiring a full file override.

---

## File Backups

MCodePatcher automatically creates backups before patch writes and restoration operations.

Stored in:

text file_backups/<version>-backups 

Features include:

- Viewing current and historical versions side-by-side
- Opening backups directly
- Revealing backups in the file system
- Switching to a backup without deleting current work

---

## Colour Indicators

| Colour | Meaning |
|---------|---------|
| Green | Healthy or applied |
| Red | Missing |
| Yellow | Modified, stale, or out of sync |
| Orange | Disabled |
| Grey | Unused |
| Cyan | Mirrored or linked |

---

## Shareable Patch Packs

Advanced rules are stored as standalone .inject.json files, making them easy to share between users and projects.

A patch pack can be as simple as a folder of advanced rule files:

```text
advanced_snippet_injections/
├─ custom_ai.inject.json
├─ better_rendering.inject.json
└─ networking_fix.inject.json
```

Users can copy those files into their own patch tree, review or edit the targets in MCodePatcher, and apply the same transformations to their workspace.

Because advanced rules can target workspace-relative files or directories, a shared rule can modify many eligible generated files without requiring users to replace entire source files.

This makes it possible to distribute reusable MCreator code customizations as small, inspectable patch definitions rather than large generated-file forks. You could theoretically create entire MCreator plugins or generators with these.

---

## Support

Discord: https://discord.gg/sQQPZQSEpS

---

## Credits

Developed by Vllax in Python (Codex-assisted).
