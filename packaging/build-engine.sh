#!/usr/bin/env bash
# Builds the engine and the document tools for the AppImage. Runs inside an Ubuntu 22.04
# container (see build-appimage.sh) so the result works on that glibc and anything newer.
#   output: /out/engine (frozen engine), /out/tools/{bin,libexec,lib} (pandoc, typst, poppler)
set -euo pipefail
PANDOC_VERSION=3.11
TYPST_VERSION=0.15.1
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends ca-certificates curl xz-utils poppler-utils binutils file >/dev/null
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh >/dev/null
export UV_PYTHON_INSTALL_DIR=/opt/python UV_LINK_MODE=copy
uv venv -q --python 3.12 /opt/venv
uv pip install -q --python /opt/venv/bin/python /src/engine "pyinstaller==6.22.3"

rm -rf /out/engine /out/tools /work && mkdir -p /out /work
cp -r /src/packaging /work/packaging
cd /work/packaging
ln -s /src/engine ../engine
/opt/venv/bin/pyinstaller --noconfirm --clean --distpath /out --workpath /work/build livery-engine.spec >/dev/null
echo "engine: $(du -sh /out/engine | cut -f1)"

# --- pandoc and typst: static release binaries
mkdir -p /out/tools/bin /out/tools/libexec /out/tools/lib
curl -Ls "https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-linux-amd64.tar.gz" | tar -xz -C /work
cp "/work/pandoc-${PANDOC_VERSION}/bin/pandoc" /out/tools/bin/pandoc
curl -Ls "https://github.com/typst/typst/releases/download/v${TYPST_VERSION}/typst-x86_64-unknown-linux-musl.tar.xz" | tar -xJ -C /work
cp /work/typst-x86_64-unknown-linux-musl/typst /out/tools/bin/typst

# --- poppler: binaries with their libraries; wrappers point the loader at lib/
for tool in pdftotext pdfimages pdffonts pdftoppm; do
  cp "$(command -v $tool)" /out/tools/libexec/
  ldd "$(command -v $tool)" | awk '/=> \//{print $3}' | while read -r lib; do
    case "$(basename "$lib")" in
      libc.so*|libm.so*|libpthread.so*|libdl.so*|librt.so*|libresolv.so*|ld-linux*) ;;  # from the host
      *) cp -n "$lib" /out/tools/lib/ ;;
    esac
  done
  cat > "/out/tools/bin/$tool" <<WRAP
#!/bin/sh
d=\$(dirname "\$(readlink -f "\$0")")
LD_LIBRARY_PATH="\$d/../lib\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}" exec "\$d/../libexec/$tool" "\$@"
WRAP
  chmod +x "/out/tools/bin/$tool"
done
cp /usr/share/doc/poppler-utils/copyright /out/tools/POPPLER-COPYRIGHT 2>/dev/null || true
for t in pandoc typst pdftotext pdftoppm; do echo "$t: $(/out/tools/bin/$t --version 2>&1 | head -1)"; done
chown -R "${HOST_UID:-0}:${HOST_GID:-0}" /out
