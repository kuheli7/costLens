"""Build the Lambda deployment zip (build/costlens-lambda.zip).

    python scripts/build_lambda.py            # template digests only
    python scripts/build_lambda.py --with-ai  # also bundle the Anthropic SDK

Dependencies are fetched as Linux wheels for the Lambda runtime (Python 3.12, x86_64), so the
zip can be built on Windows or macOS. boto3 is already in the Lambda runtime and is left out.
Lambda's direct-upload limits are 50 MB zipped and 250 MB unzipped; the script reports both.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
PKG = BUILD / "pkg"
ZIP = BUILD / "costlens-lambda.zip"
ZIPPED_LIMIT_MB = 50
UNZIPPED_LIMIT_MB = 250


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-ai", action="store_true", help="bundle the anthropic SDK")
    ap.add_argument("--python-version", default="3.12")
    ap.add_argument("--platform", default="manylinux2014_x86_64")
    args = ap.parse_args()

    if PKG.exists():
        shutil.rmtree(PKG)
    PKG.mkdir(parents=True)
    deps = ["duckdb>=1.0"] + (["anthropic>=0.40"] if args.with_ai else [])
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install", "--quiet", "--target", str(PKG),
            "--platform", args.platform, "--implementation", "cp",
            "--python-version", args.python_version, "--only-binary=:all:", *deps,
        ],
        check=True,
    )  # fmt: skip
    shutil.copytree(ROOT / "src" / "costlens", PKG / "costlens", ignore=shutil.ignore_patterns("__pycache__"))

    if ZIP.exists():
        ZIP.unlink()
    unzipped = 0
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(PKG.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                z.write(path, path.relative_to(PKG).as_posix())
                unzipped += path.stat().st_size
    zipped_mb, unzipped_mb = ZIP.stat().st_size / 1e6, unzipped / 1e6
    print(f"{ZIP.relative_to(ROOT)}: {zipped_mb:.1f} MB zipped, {unzipped_mb:.1f} MB unzipped")
    if zipped_mb > ZIPPED_LIMIT_MB or unzipped_mb > UNZIPPED_LIMIT_MB:
        print("WARNING: over Lambda's direct-upload limit; use S3 upload or a layer", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
