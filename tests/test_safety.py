"""Safety helper unit tests."""

from synapse.safety import check_command


def test_blacklist_blocks_force_push():
    verdict = check_command("git push --force origin main")
    assert verdict.allowed is False


def test_blacklist_blocks_powershell_recursive_delete():
    verdict = check_command("Remove-Item -Recurse -Force C:\\temp\\x")
    assert verdict.allowed is False


def test_empty_command_rejected():
    verdict = check_command("   ")
    assert verdict.allowed is False
