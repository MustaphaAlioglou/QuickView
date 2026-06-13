# Code review — uncommitted working-tree changes

- **Date:** 2026-06-12
- **Scope:** `git diff HEAD` on `master` (changes after commit e803e88: socket move to XDG_RUNTIME_DIR, NUL wire protocol, byte-bounded memory cache, amortized cache pruning, corrupt-cache eviction, media-player signal cleanup, `QUICKVIEW_STRICT_SANDBOX`, Exec quoting)
- **Method:** /code-review high — 7 finder angles, 24 raw candidates, 7 verified after dedup: 4 confirmed, 1 plausible, 2 refuted

## Findings (most severe first)

### 1. Upgrade leaves the old daemon running — `install.sh:41` (CONFIRMED)
`systemctl --user enable --now` does not restart an already-running service, while this
diff moves the socket from `/tmp/quickview-UID` to `$XDG_RUNTIME_DIR/quickview-UID`
**and** changes the wire protocol from newline- to NUL-joined.

**Failure scenario:** user with the daemon running re-runs `install.sh` to upgrade →
old daemon keeps listening on the old `/tmp` socket, new invocations compute the new
path, fail to connect, and spawn a second daemon → two daemons, duplicate preview
windows, and the stale daemon never exits. Nothing kills it or removes the old socket.

**Suggested fix:** add `systemctl --user try-restart quickview.service` (or `restart`)
to `install.sh`.

### 2. Guard timer makes the daemon act on truncated messages — `quickview.py:958` (CONFIRMED)
The 2-second guard's `conn.disconnectFromServer()` fires the `disconnected` signal,
so `on_done()` processes the partial buffer as a complete message instead of
discarding the interrupted request.

**Failure scenario:** a legitimately slow client (huge multi-file selection on a
loaded system) takes >2 s to finish sending → guard closes mid-transfer →
`on_done()` splits the partial NUL-joined buffer and calls `viewer.show_files()`
with a half-received garbage path. `on_done()` cannot distinguish a clean client
close from a guard-forced one.

**Suggested fix:** have the guard set an `aborted` flag (or disconnect `on_done`)
before closing, so a forced close drops the buffer.

### 3. Failed prune suppresses pruning for a long time — `quickview.py:166` (CONFIRMED)
`_unpruned_bytes` is reset to 0 *before* `prune_cache()` runs, and `prune_cache()`
returns silently on `OSError`.

**Failure scenario:** prune hits an `OSError` while scanning the cache dir → nothing
is deleted, but the counter was already zeroed → on a mostly-cache-hit workload the
disk cache stays over `CACHE_CAP_BYTES` until another 32 MiB of writes accumulates
(hours or days).

**Suggested fix:** reset the counter only after a successful prune.

### 4. IPC contract duplicated across client and daemon — `client.py:14` (CONFIRMED, cleanup)
`socket_path()`, the NUL-join protocol, and `file://` URL decoding are duplicated
verbatim between `client.py` and `quickview.py`, coordinated only by
"must stay in lockstep" comments.

**Cost:** a future change to any of the three lands in one file and not the other →
the client silently misses the daemon and spawns duplicates, or paths get mangled.
A small PySide6-free shared module (e.g. `ipc.py`) importable by both would make
drift impossible — `client.py` only uses stdlib, so this is structurally feasible.

### 5. Redundant `_mem_cache_bytes` counter — `quickview.py:313` (PLAUSIBLE, cleanup)
The byte total is derivable from the `nbytes` already stored in each cache tuple.
It is currently updated correctly at all three mutation sites, but any future
mutation of `_mem_cache` that misses the counter silently breaks the byte bound.

**Cost:** the dict holds at most ~10–50 screen-sized pixmaps, so recomputing
`sum(nb for *_, nb in self._mem_cache.values())` on insert is microseconds;
the manual counter trades that negligible cost for a drift foot-gun in a
long-lived daemon.

## Refuted candidates (for the record)

- **`human_size()` missing trailing return** (flagged by 4 angles): the deleted
  `return f"{n} B"` after the loop was provably dead code — the `unit == "TB"`
  branch returns unconditionally on the final iteration, so the function cannot
  return `None` for any input. Removal is correct.
- **Quoted `Exec=`/`ExecStart=` lines invalid**: the Desktop Entry Specification
  explicitly allows whole-argument quoting, and `%U` sits outside the quotes as
  its own argument, as required; systemd likewise supports double-quoted
  `ExecStart` paths. The quoting change is valid in all three generated files.

## Raw findings JSON

```json
[
  {
    "file": "install.sh",
    "line": 41,
    "summary": "Upgrading leaves the old daemon running on the old /tmp socket: install.sh uses `systemctl --user enable --now`, which does not restart an already-running service, while the diff moves the socket to XDG_RUNTIME_DIR and changes the wire protocol from newline- to NUL-joined.",
    "failure_scenario": "User with the daemon running re-runs install.sh to upgrade → old daemon keeps listening on /tmp/quickview-UID, new client/daemon invocations compute /run/user/UID/quickview-UID, fail to connect, and spawn a second daemon → two daemons, duplicate preview windows, and the stale daemon never exits; nothing in the diff or install.sh kills it or removes the old socket."
  },
  {
    "file": "quickview.py",
    "line": 958,
    "summary": "The 2-second guard timer's disconnectFromServer() fires the `disconnected` signal, so on_done() processes the truncated buffer as a complete message instead of discarding the interrupted request.",
    "failure_scenario": "A legitimately slow client (huge multi-file selection on a loaded system) takes >2s to finish sending → guard closes the connection mid-transfer → on_done() splits the partial NUL-joined buffer and calls viewer.show_files() with a half-received garbage path; on_done() has no way to distinguish a clean client close from a guard-forced one."
  },
  {
    "file": "quickview.py",
    "line": 166,
    "summary": "_unpruned_bytes is reset to 0 before prune_cache() runs, and prune_cache() returns silently on OSError, so a failed prune leaves an over-cap cache unpruned until another 32 MiB of writes accumulates.",
    "failure_scenario": "prune_cache() hits an OSError while scanning the cache dir → returns without deleting anything, but the counter was already zeroed → on a mostly-cache-hit workload (few writes) the disk cache stays over CACHE_CAP_BYTES for hours or days; move the reset after a successful prune."
  },
  {
    "file": "client.py",
    "line": 14,
    "summary": "The full client↔daemon IPC contract — socket_path(), the NUL-join protocol, and file:// URL decoding — is duplicated verbatim between client.py and quickview.py, coordinated only by 'must stay in lockstep' comments.",
    "failure_scenario": "A future change to any of the three (new env var in path derivation, protocol framing fix, URL-decode bugfix) lands in one file and not the other → client silently misses the daemon and spawns duplicates, or paths get mangled; a small PySide6-free shared module (e.g. ipc.py) importable by both would make drift impossible — client.py only uses stdlib, so this is structurally feasible."
  },
  {
    "file": "quickview.py",
    "line": 313,
    "summary": "_mem_cache_bytes is redundant state derivable from the nbytes already stored in each cache tuple; it is currently updated correctly at all three mutation sites, but any future mutation of _mem_cache that misses the counter silently breaks the byte bound.",
    "failure_scenario": "The dict holds at most ~10-50 screen-sized pixmaps, so recomputing sum(nb for *_ , nb in self._mem_cache.values()) on insert is microseconds; keeping the manual counter trades that negligible cost for a drift foot-gun in a long-lived daemon where a leak means unbounded pixmap memory."
  }
]
```
