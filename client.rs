// QuickView — a Quick Look style file previewer for KDE Plasma.
// Copyright (C) 2026 Mustapha Alioglou
//
// This program is free software: you can redistribute it and/or modify it
// under the terms of the GNU General Public License as published by the
// Free Software Foundation, either version 3 of the License, or (at your
// option) any later version. This program is distributed WITHOUT ANY
// WARRANTY; see the LICENSE file, or <https://www.gnu.org/licenses/>.

//! Fast path for QuickView: hand the file paths to the running daemon.
//!
//! Starting a Python interpreter costs ~29 ms before the daemon even
//! learns there is a file to show; this costs under one. Exits 0 if a
//! daemon accepted the paths, 1 otherwise — the launcher then starts
//! quickview.py, which becomes the daemon.
//!
//! This program deliberately parses nothing. Filenames are untrusted
//! input (a crafted name inside a downloaded archive is a plausible
//! vector) and this is the one part of QuickView that runs outside the
//! bubblewrap jail, so percent-decoding and path normalization live in
//! ipc.py on the daemon side instead. Here the arguments are forwarded
//! byte for byte. See ipc.py for the wire format.
//!
//! No dependencies, so `rustc -O client.rs` builds it without Cargo.

use std::env;
use std::io::Write;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::net::UnixStream;
use std::process::ExitCode;
use std::time::Duration;

fn socket_path() -> std::path::PathBuf {
    // XDG_RUNTIME_DIR is stable for the whole session; TMPDIR is not (it
    // may be set in a terminal but not in the systemd unit, or vice versa).
    let run = env::var_os("XDG_RUNTIME_DIR").unwrap_or_else(|| "/tmp".into());
    // std has no getuid(), and pulling in libc for one number is not worth
    // a dependency: on Linux /proc/self is owned by the calling process's
    // real uid, which is the number ipc.socket_path() formats in.
    let uid = std::fs::metadata("/proc/self")
        .map(|m| std::os::unix::fs::MetadataExt::uid(&m))
        .unwrap_or(0);
    std::path::PathBuf::from(run).join(format!("quickview-{uid}"))
}

fn run() -> Option<()> {
    // args_os, not args: filenames are bytes, not guaranteed UTF-8, and
    // args() would panic on a name that is not. Nothing here decodes them.
    let args: Vec<std::ffi::OsString> = env::args_os().skip(1).collect();
    if args.is_empty() || args.iter().any(|a| a.as_bytes().starts_with(b"-")) {
        return None; // no file, or a flag (--daemon, --clear-cache): quickview.py handles it
    }

    // Field 0 is our working directory: the daemon resolves relative
    // arguments against it, since its own cwd (systemd, typically $HOME)
    // is not ours.
    let cwd = env::current_dir().ok()?;
    let mut msg: Vec<u8> = Vec::new();
    msg.extend_from_slice(cwd.as_os_str().as_bytes());
    for arg in &args {
        msg.push(0); // NUL: the one byte POSIX forbids in a path
        msg.extend_from_slice(arg.as_bytes());
    }

    let mut sock = UnixStream::connect(socket_path()).ok()?;
    sock.set_write_timeout(Some(Duration::from_millis(500))).ok()?;
    sock.write_all(&msg).ok()?;
    // Dropping the stream closes it, and that close is the message framing.
    Some(())
}

fn main() -> ExitCode {
    match run() {
        Some(()) => ExitCode::SUCCESS,
        None => ExitCode::FAILURE,
    }
}
