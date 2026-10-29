import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
os.environ["PATH"] = f"{ROOT / '.tools' / 'bin'}{os.pathsep}{os.environ['PATH']}"


@pytest.fixture(scope="session", autouse=True)
def fixtures_built():
    if not (FIXTURES / "features.docx").exists():
        subprocess.run([sys.executable, str(ROOT / "tests" / "make_fixtures.py")], check=True, cwd=ROOT)
    yield


@pytest.fixture(scope="session")
def built_features(tmp_path_factory):
    """Build features.docx once for the whole session."""
    from livery.cli import main

    out = tmp_path_factory.mktemp("out")
    rc = main(["build", str(FIXTURES / "features.docx"), "--out", str(out)])
    return rc, out / "features"
