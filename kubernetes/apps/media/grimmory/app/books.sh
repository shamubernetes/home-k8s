#!/bin/sh
# Stage the actual shared Books tree once, including the bookdrop subdirectory.
# Repeated byte/metadata equality detects writes; it does not fence other apps.
set -eu
umask 077
books=${GRIMMORY_BOOKS:-/books}
bookdrop=${GRIMMORY_BOOKDROP:-/bookdrop}
output=${1:?pass private staging archive path}
[ -d "$books" ] && [ ! -L "$books" ] || exit 1
[ -d "$bookdrop" ] && [ ! -L "$bookdrop" ] || exit 1
# The two live mounts refer to the same subtree, not two independent payloads.
[ -d "$books/bookdrop" ] && [ ! -L "$books/bookdrop" ] || exit 1
[ "$books/bookdrop" -ef "$bookdrop" ] || exit 1
[ ! -e "$output" ] && [ ! -L "$output" ] || exit 1
work=$(mktemp -d "$(dirname "$output")/.books.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
archive() {
  # Never follow links, accept device nodes, or silently omit unreadable files.
  # Keep pathnames private even on a failed source scan or tar invocation.
  if ! find "$books" \( ! -type f -a ! -type d \) -print >"$work/unsafe" 2>"$work/errors"; then
    printf 'Books source scan failed\n' >&2; return 1
  fi
  [ ! -s "$work/unsafe" ] || { printf 'Books special entry refused\n' >&2; return 1; }
  if ! find "$books" -type f \( -links +1 -o -perm -4000 -o -perm -2000 \) -print >"$work/unsafe" 2>"$work/errors"; then
    printf 'Books file scan failed\n' >&2; return 1
  fi
  [ ! -s "$work/unsafe" ] || { printf 'Books linked or privileged file refused\n' >&2; return 1; }
  if ! tar -C "$books" -cf "$1" . 2>"$work/errors"; then
    printf 'Books archive failed\n' >&2; return 1
  fi
  # GNU and BusyBox tar can emit warnings without failing. Refuse those too.
  [ ! -s "$work/errors" ] || { printf 'Books archive warning refused\n' >&2; return 1; }
  [ -s "$1" ]
}
archive "$work/first.tar"
archive "$work/second.tar"
cmp -s "$work/first.tar" "$work/second.tar" || { printf 'Books changed during capture\n' >&2; exit 1; }
mv "$work/first.tar" "$output"
