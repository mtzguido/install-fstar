#!/usr/bin/env bash
# Run inside the selected OPAM environment, including Cygwin on Windows.
set -euo pipefail
cd "$1"
archive=$2
jobs=$3
# Dependencies were reconciled and fingerprinted before the package cache lookup.
make_command='make'
if [[ "$(uname -s)" == Darwin ]]; then
  make_command=gmake
fi
# Some portable source distributions contain the Custard interface but their
# install_fstarc recipe omits it. Plugins need the exact interface used to
# extract this compiler. Mirror mk/stage.mk's layout; legacy sources lack it.
if [[ -f dune/fstar-guts/fstarc.ml/fstarc.cui ]]; then
  mkdir -p out/lib/fstar/fstarc
  cp dune/fstar-guts/fstarc.ml/fstarc.cui out/lib/fstar/fstarc/fstarc.cui
fi
if [[ "${OS:-}" == Windows_NT ]]; then
  # Find the actual DLL used by the compiler; PATH may also contain a 32-bit one.
  opam exec -- "$make_command" -j"$jobs" ADMIT=1
  compiler=out/bin/fstar.exe
  if [[ ! -f "$compiler" ]]; then
    compiler=stage3/out/bin/fstar.exe
  fi
  if [[ ! -f "$compiler" ]]; then
    compiler=stage2/out/bin/fstar.exe
  fi
  LIBGMP=$(opam exec -- ldd "$compiler" | sed -n 's/^[[:space:]]*libgmp-10.dll => *\([^ ]*\) .*/\1/p')
  test -f "$LIBGMP"
  export LIBGMP
fi
opam exec -- "$make_command" -j"$jobs" package \
  ADMIT=1 FSTAR_TAG= FSTAR_PACKAGE_FORMAT=tar.gz FSTAR_PACKAGE_Z3=true
mv fstar.tar.gz "$archive"
