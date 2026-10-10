import re
from pathlib import Path

from nevoflux_muse import __version__

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("browser_snapshot", "browser_get_markdown", "browser_screenshot", "browser_get_tabs",
         "browser_navigate", "browser_wait_for", "browser_click_by_id", "browser_fill_by_id",
         "browser_type_by_id", "browser_click", "browser_fill", "browser_type",
         "browser_key_press", "browser_scroll", "browser_activate_tab", "browser_upload_file")
CODES = ("unknown_tool", "tier_denied", "approval_denied", "approval_timeout",
         "browser_unavailable", "no_agent_tab", "tab_closed", "tool_failed",
         "approval_expired")


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_this_is_0_3_0():
    assert __version__ == "0.3.0"


def test_pyproject_matches_the_package():
    found = re.search(r'^version = "([^"]+)"$', read("pyproject.toml"), re.MULTILINE)
    assert found and found.group(1) == __version__


def test_install_docs_name_this_version():
    for name in ("INSTALL.md", "README.md"):
        assert f"-b v{__version__} https://" in read(name), name
    install = read("INSTALL.md")
    assert f'{{"client": "{__version__}"' in install
    assert f"nevoflux-muse {__version__} (protocol 1-1)" in install


def test_install_documents_approvals_and_error_codes():
    install = read("INSTALL.md")
    assert "## Handling approvals (exit code 3)" in install
    assert "and 3 means NevoFlux asks for an" in install  # the Conventions bullet
    for command in ("nf approve", "nf deny", "nf pending --json"):
        assert command in install
    for code in CODES:
        assert f"`{code}`" in install, code
    for tool in TOOLS:
        assert f"`{tool}`" in install, tool
