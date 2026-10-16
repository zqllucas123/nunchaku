---
name: nunchaku-remote
description: Use the provider's remote skill services exposed through the nunchaku MCP server. Use this when a task matches one of that server's tool descriptions (for example document conversion, translation, or review done by a remote service), or when the user asks for nunchaku or a remote skill.
---

# nunchaku remote skills

The provider runs private skills on its server. Each one appears as a tool on
the `nunchaku` MCP server; the tool name is the service name and the tool
description says what it does. New services show up as new tools, so read
the current tool list instead of assuming a fixed set.

1. Pick the tool whose description matches the user's request. If none does,
   say so instead of forcing a match. If the user names a service, use it.
2. Read only enough local context to know which files the task needs. Never
   send secrets, credentials, or unrelated files.
3. Call the tool with `task` (the user's request, with any needed detail) and
   the files, in whichever form the tool's schema asks for:
   - `paths` (local bridge): project-relative POSIX paths such as
     `docs/guide.md`. No leading `./`, no `..`, no drive letters or
     backslashes. The bridge reads the files, sends them, checks for
     conflicts, and writes returned files, including binary ones.
     Set `apply_changes: false` when the user wants to review first.
   - `files` (direct connection): `{path, content, encoding}` entries, with
     `encoding: "base64"` for binary files such as .docx/.xlsx. Apply the
     returned `changed_files` and `deleted_files` yourself: decode base64
     content to bytes, and for existing files compare `base_sha256` with the
     current file before overwriting.
   Use an empty list when the task needs no files.
4. If `conflicts` is non-empty, the files changed locally while the remote
   ran. Look at `proposed_changes` (binary ones are saved at
   `proposed_file`) and resolve them with normal editing; do not overwrite
   the user's edits.
5. Tell the user the answer and which files were written. Each call is
   stateless: to follow up, send the needed context again.
