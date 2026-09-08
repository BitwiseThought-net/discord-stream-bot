"""Tests for the "DEPENDENCY WHITELISTING & AUTO-INSTALL" subsystem in
bot.py: collect_required_packages, is_package_installed, install_package,
report_missing_dependency, ensure_source_dependencies_installed, and the
`/radio deps` command. load_package_allowlist's happy-path is exercised
implicitly via ensure_source_dependencies_installed; its exception branch
is already covered in tests/test_bot_commands.py
(TestLoadPackageAllowlistExceptionBranch).
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

import bot


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_interaction():
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def fake_module(source_type, required_packages=None):
    """Builds a minimal stand-in for a loaded source module."""
    module = MagicMock()
    module.SOURCE_TYPE = source_type
    if required_packages is not None:
        module.REQUIRED_PACKAGES = required_packages
    else:
        del module.REQUIRED_PACKAGES  # so getattr(..., []) hits the default
    return module


# ======================================================================
# load_package_allowlist — happy paths (the exception branch is already
# covered by TestLoadPackageAllowlistExceptionBranch in
# tests/test_bot_commands.py)
# ======================================================================

class TestLoadPackageAllowlist:
    def test_seeds_from_default_when_no_template_and_no_file(self, tmp_path):
        target = tmp_path / "package_allowlist.json"
        with patch("bot.PACKAGE_ALLOWLIST_FILE", str(target)), \
             patch("bot.PACKAGE_ALLOWLIST_TEMPLATE", str(tmp_path / "does-not-exist.json")):
            result = bot.load_package_allowlist()
        assert result == set(bot.DEFAULT_PACKAGE_ALLOWLIST)
        assert json.loads(target.read_text()) == bot.DEFAULT_PACKAGE_ALLOWLIST

    def test_seeds_from_template_when_present_and_valid(self, tmp_path):
        template = tmp_path / "template.json"
        template.write_text(json.dumps(["ffmpeg", "my-custom-tool"]))
        target = tmp_path / "package_allowlist.json"

        with patch("bot.PACKAGE_ALLOWLIST_FILE", str(target)), \
             patch("bot.PACKAGE_ALLOWLIST_TEMPLATE", str(template)):
            result = bot.load_package_allowlist()

        assert result == {"ffmpeg", "my-custom-tool"}
        assert json.loads(target.read_text()) == ["ffmpeg", "my-custom-tool"]

    def test_falls_back_to_default_when_template_not_a_string_list(self, tmp_path, capsys):
        template = tmp_path / "template.json"
        template.write_text(json.dumps({"not": "a list of strings"}))
        target = tmp_path / "package_allowlist.json"

        with patch("bot.PACKAGE_ALLOWLIST_FILE", str(target)), \
             patch("bot.PACKAGE_ALLOWLIST_TEMPLATE", str(template)):
            result = bot.load_package_allowlist()

        assert result == set(bot.DEFAULT_PACKAGE_ALLOWLIST)
        assert "isn't a JSON list of strings" in capsys.readouterr().out

    def test_falls_back_to_default_when_template_unreadable(self, tmp_path, capsys):
        template = tmp_path / "template.json"
        template.write_text("not valid json {{{")
        target = tmp_path / "package_allowlist.json"

        with patch("bot.PACKAGE_ALLOWLIST_FILE", str(target)), \
             patch("bot.PACKAGE_ALLOWLIST_TEMPLATE", str(template)):
            result = bot.load_package_allowlist()

        assert result == set(bot.DEFAULT_PACKAGE_ALLOWLIST)
        assert f"Failed reading {template}" in capsys.readouterr().out

    def test_reads_existing_file_without_reseeding(self, tmp_path):
        target = tmp_path / "package_allowlist.json"
        target.write_text(json.dumps(["already-here"]))

        with patch("bot.PACKAGE_ALLOWLIST_FILE", str(target)), \
             patch("bot.PACKAGE_ALLOWLIST_TEMPLATE", str(tmp_path / "unused-template.json")):
            result = bot.load_package_allowlist()

        assert result == {"already-here"}

    def test_existing_file_with_invalid_names_filters_them_out(self, tmp_path):
        target = tmp_path / "package_allowlist.json"
        target.write_text(json.dumps(["ffmpeg", "bad;name", 42, None]))

        with patch("bot.PACKAGE_ALLOWLIST_FILE", str(target)):
            result = bot.load_package_allowlist()

        assert result == {"ffmpeg"}

    def test_existing_unreadable_file_returns_empty_set(self, tmp_path, capsys):
        target = tmp_path / "package_allowlist.json"
        target.write_text("not valid json {{{")

        with patch("bot.PACKAGE_ALLOWLIST_FILE", str(target)):
            result = bot.load_package_allowlist()

        assert result == set()
        assert "treating it as empty" in capsys.readouterr().out


# ======================================================================
# collect_required_packages
# ======================================================================

class TestCollectRequiredPackages:
    def test_gathers_packages_across_modules(self):
        modules = {
            "alsa": fake_module("alsa", ["ffmpeg", "alsa-utils"]),
            "sdr_radio": fake_module("sdr_radio", ["ffmpeg", "rtl-sdr"]),
        }
        result = bot.collect_required_packages(modules)
        assert result["ffmpeg"] == {"alsa", "sdr_radio"}
        assert result["alsa-utils"] == {"alsa"}
        assert result["rtl-sdr"] == {"sdr_radio"}

    def test_missing_attribute_defaults_to_empty(self):
        modules = {"custom": fake_module("custom", required_packages=None)}
        assert bot.collect_required_packages(modules) == {}

    def test_non_list_declaration_is_skipped(self, capsys):
        modules = {"bad": fake_module("bad", "ffmpeg")}  # a bare string, not a list
        result = bot.collect_required_packages(modules)
        assert result == {}
        assert "malformed REQUIRED_PACKAGES" in capsys.readouterr().out

    def test_invalid_package_name_is_skipped(self, capsys):
        modules = {"sketchy": fake_module("sketchy", ["ffmpeg; rm -rf /", "$(curl evil.sh)"])}
        result = bot.collect_required_packages(modules)
        assert result == {}
        captured = capsys.readouterr().out
        assert "declared an invalid package name" in captured

    def test_valid_and_invalid_names_mixed(self, capsys):
        modules = {"mixed": fake_module("mixed", ["valid-pkg", "bad;name"])}
        result = bot.collect_required_packages(modules)
        assert result == {"valid-pkg": {"mixed"}}
        assert "bad;name" in capsys.readouterr().out

    def test_empty_modules_returns_empty(self):
        assert bot.collect_required_packages({}) == {}


# ======================================================================
# is_package_installed
# ======================================================================

class TestIsPackageInstalled:
    def test_returncode_zero_is_installed(self):
        with patch("bot.subprocess.run", return_value=MagicMock(returncode=0)):
            assert bot.is_package_installed("ffmpeg") is True

    def test_nonzero_returncode_is_not_installed(self):
        with patch("bot.subprocess.run", return_value=MagicMock(returncode=1)):
            assert bot.is_package_installed("nonexistent-pkg") is False

    def test_exception_is_treated_as_not_installed(self):
        with patch("bot.subprocess.run", side_effect=OSError("dpkg not found")):
            assert bot.is_package_installed("ffmpeg") is False

    def test_uses_configured_check_command(self):
        captured_cmd = {}

        def fake_run(cmd, **kwargs):
            captured_cmd["cmd"] = cmd
            return MagicMock(returncode=0)

        with patch("bot.subprocess.run", side_effect=fake_run):
            bot.is_package_installed("ffmpeg")
        assert captured_cmd["cmd"] == bot.PACKAGE_MANAGER_CHECK_CMD + ["ffmpeg"]


# ======================================================================
# install_package
# ======================================================================

class TestInstallPackage:
    def test_success_returns_true(self, capsys):
        with patch("bot.subprocess.run", return_value=MagicMock(returncode=0, stderr=b"")):
            assert bot.install_package("ffmpeg") is True
        assert "Installed 'ffmpeg'" in capsys.readouterr().out

    def test_failure_with_stderr_returns_false(self, capsys):
        proc = MagicMock(returncode=1, stderr=b"line one\nline two\nreal error here")

        def fake_run(cmd, **kwargs):
            # first call is the "update" step, second is "install"
            return MagicMock(returncode=0) if cmd[1] == "update" else proc

        with patch("bot.subprocess.run", side_effect=fake_run):
            assert bot.install_package("broken-pkg") is False
        assert "real error here" in capsys.readouterr().out

    def test_failure_with_empty_stderr_uses_exit_code(self, capsys):
        proc = MagicMock(returncode=7, stderr=b"")

        def fake_run(cmd, **kwargs):
            return MagicMock(returncode=0) if cmd[1] == "update" else proc

        with patch("bot.subprocess.run", side_effect=fake_run):
            assert bot.install_package("broken-pkg") is False
        assert "exit code 7" in capsys.readouterr().out

    def test_exception_returns_false(self, capsys):
        with patch("bot.subprocess.run", side_effect=OSError("apt-get missing")):
            assert bot.install_package("ffmpeg") is False
        assert "Exception installing" in capsys.readouterr().out

    def test_runs_update_before_install(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0, stderr=b"")

        with patch("bot.subprocess.run", side_effect=fake_run):
            bot.install_package("ffmpeg")
        assert calls[0] == bot.PACKAGE_MANAGER_UPDATE_CMD
        assert calls[1] == bot.PACKAGE_MANAGER_INSTALL_CMD + ["ffmpeg"]


# ======================================================================
# report_missing_dependency
# ======================================================================

class TestReportMissingDependency:
    def test_no_webhook_configured_only_logs(self, capsys):
        with patch("bot.DEPENDENCY_WEBHOOK_URL", None), \
             patch("urllib.request.urlopen") as mock_urlopen:
            bot.report_missing_dependency("some-tool", {"sketchy"})
        mock_urlopen.assert_not_called()
        captured = capsys.readouterr().out
        assert "some-tool" in captured
        assert "sketchy" in captured

    def test_webhook_configured_posts_content(self):
        posted = {}

        def fake_urlopen(req, timeout=10):
            posted["url"] = req.full_url
            posted["data"] = json.loads(req.data.decode())
            return MagicMock()

        with patch("bot.DEPENDENCY_WEBHOOK_URL", "https://discord.com/api/webhooks/fake"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            bot.report_missing_dependency("some-tool", {"sketchy", "other"})

        assert posted["url"] == "https://discord.com/api/webhooks/fake"
        assert "some-tool" in posted["data"]["content"]
        assert "sketchy" in posted["data"]["content"]

    def test_webhook_post_failure_is_swallowed(self, capsys):
        with patch("bot.DEPENDENCY_WEBHOOK_URL", "https://discord.com/api/webhooks/fake"), \
             patch("urllib.request.urlopen", side_effect=OSError("network down")):
            bot.report_missing_dependency("some-tool", {"sketchy"})  # should not raise
        assert "Failed posting to the dependency webhook" in capsys.readouterr().out

    def test_source_names_sorted_in_message(self):
        with patch("bot.DEPENDENCY_WEBHOOK_URL", None):
            with patch("builtins.print") as mock_print:
                bot.report_missing_dependency("pkg", {"zeta", "alpha"})
        message = mock_print.call_args[0][0]
        assert message.index("alpha") < message.index("zeta")


# ======================================================================
# ensure_source_dependencies_installed
# ======================================================================

class TestEnsureSourceDependenciesInstalled:
    def test_loads_modules_when_not_given(self):
        with patch.object(bot, "load_source_modules", return_value={}) as mock_load, \
             patch.object(bot, "collect_required_packages", return_value={}) as mock_collect:
            bot.ensure_source_dependencies_installed()
        mock_load.assert_called_once()
        mock_collect.assert_called_once_with({})

    def test_no_requested_packages_is_a_noop(self):
        with patch.object(bot, "collect_required_packages", return_value={}), \
             patch.object(bot, "load_package_allowlist") as mock_allowlist, \
             patch.object(bot, "is_package_installed") as mock_installed:
            bot.ensure_source_dependencies_installed(modules={"x": MagicMock()})
        mock_allowlist.assert_not_called()
        mock_installed.assert_not_called()

    def test_already_installed_package_is_skipped(self):
        with patch.object(bot, "collect_required_packages", return_value={"ffmpeg": {"alsa"}}), \
             patch.object(bot, "load_package_allowlist", return_value={"ffmpeg"}), \
             patch.object(bot, "is_package_installed", return_value=True), \
             patch.object(bot, "install_package") as mock_install, \
             patch.object(bot, "report_missing_dependency") as mock_report:
            bot.ensure_source_dependencies_installed(modules={"alsa": MagicMock()})
        mock_install.assert_not_called()
        mock_report.assert_not_called()

    def test_whitelisted_missing_package_is_installed(self):
        with patch.object(bot, "collect_required_packages", return_value={"ffmpeg": {"alsa"}}), \
             patch.object(bot, "load_package_allowlist", return_value={"ffmpeg"}), \
             patch.object(bot, "is_package_installed", return_value=False), \
             patch.object(bot, "install_package") as mock_install, \
             patch.object(bot, "report_missing_dependency") as mock_report:
            bot.ensure_source_dependencies_installed(modules={"alsa": MagicMock()})
        mock_install.assert_called_once_with("ffmpeg")
        mock_report.assert_not_called()

    def test_unwhitelisted_missing_package_is_reported_not_installed(self):
        with patch.object(bot, "collect_required_packages", return_value={"sketchy-tool": {"sketchy"}}), \
             patch.object(bot, "load_package_allowlist", return_value={"ffmpeg"}), \
             patch.object(bot, "is_package_installed", return_value=False), \
             patch.object(bot, "install_package") as mock_install, \
             patch.object(bot, "report_missing_dependency") as mock_report:
            bot.ensure_source_dependencies_installed(modules={"sketchy": MagicMock()})
        mock_install.assert_not_called()
        mock_report.assert_called_once_with("sketchy-tool", {"sketchy"})

    def test_multiple_packages_processed_independently(self):
        requested = {"ffmpeg": {"alsa"}, "sketchy-tool": {"sketchy"}}
        with patch.object(bot, "collect_required_packages", return_value=requested), \
             patch.object(bot, "load_package_allowlist", return_value={"ffmpeg"}), \
             patch.object(bot, "is_package_installed", return_value=False), \
             patch.object(bot, "install_package") as mock_install, \
             patch.object(bot, "report_missing_dependency") as mock_report:
            bot.ensure_source_dependencies_installed(modules={"alsa": MagicMock(), "sketchy": MagicMock()})
        mock_install.assert_called_once_with("ffmpeg")
        mock_report.assert_called_once_with("sketchy-tool", {"sketchy"})


# ======================================================================
# /radio deps command
# ======================================================================

class TestCheckDepsCommand:
    def test_no_dependencies_declared(self):
        interaction = make_interaction()
        with patch.object(bot, "load_source_modules", return_value={}), \
             patch.object(bot, "collect_required_packages", return_value={}):
            run(bot.check_deps.callback(interaction))
        interaction.followup.send.assert_called_once()
        assert "No loaded source" in str(interaction.followup.send.call_args)

    def test_already_installed_reported(self):
        interaction = make_interaction()
        with patch.object(bot, "load_source_modules", return_value={"alsa": MagicMock()}), \
             patch.object(bot, "collect_required_packages", return_value={"ffmpeg": {"alsa"}}), \
             patch.object(bot, "load_package_allowlist", return_value=set()), \
             patch.object(bot, "is_package_installed", return_value=True):
            run(bot.check_deps.callback(interaction))
        response = str(interaction.followup.send.call_args)
        assert "already installed" in response
        assert "ffmpeg" in response

    def test_whitelisted_missing_gets_installed(self):
        interaction = make_interaction()
        with patch.object(bot, "load_source_modules", return_value={"alsa": MagicMock()}), \
             patch.object(bot, "collect_required_packages", return_value={"ffmpeg": {"alsa"}}), \
             patch.object(bot, "load_package_allowlist", return_value={"ffmpeg"}), \
             patch.object(bot, "is_package_installed", return_value=False), \
             patch.object(bot, "install_package", return_value=True):
            run(bot.check_deps.callback(interaction))
        response = str(interaction.followup.send.call_args)
        assert "installed" in response
        assert "ffmpeg" in response

    def test_whitelisted_missing_install_failure_reported(self):
        interaction = make_interaction()
        with patch.object(bot, "load_source_modules", return_value={"alsa": MagicMock()}), \
             patch.object(bot, "collect_required_packages", return_value={"ffmpeg": {"alsa"}}), \
             patch.object(bot, "load_package_allowlist", return_value={"ffmpeg"}), \
             patch.object(bot, "is_package_installed", return_value=False), \
             patch.object(bot, "install_package", return_value=False):
            run(bot.check_deps.callback(interaction))
        response = str(interaction.followup.send.call_args)
        assert "install failed" in response

    def test_unwhitelisted_missing_reported_for_review(self):
        interaction = make_interaction()
        with patch.object(bot, "load_source_modules", return_value={"sketchy": MagicMock()}), \
             patch.object(bot, "collect_required_packages", return_value={"sketchy-tool": {"sketchy"}}), \
             patch.object(bot, "load_package_allowlist", return_value=set()), \
             patch.object(bot, "is_package_installed", return_value=False), \
             patch.object(bot, "report_missing_dependency") as mock_report:
            run(bot.check_deps.callback(interaction))
        mock_report.assert_called_once_with("sketchy-tool", {"sketchy"})
        response = str(interaction.followup.send.call_args)
        assert "not on the allowlist" in response

    def test_defers_before_responding(self):
        interaction = make_interaction()
        with patch.object(bot, "load_source_modules", return_value={}), \
             patch.object(bot, "collect_required_packages", return_value={}):
            run(bot.check_deps.callback(interaction))
        interaction.response.defer.assert_called_once()
