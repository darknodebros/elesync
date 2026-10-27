# Homebrew formula for EleSync.
#
# Usage (once the tap repo darknodebros/homebrew-elesync exists):
#   brew tap darknodebros/elesync
#   brew install elesync
#
# This formula installs the `ele` CLI and MCP server via pipx (isolated env).
# The tap repo (https://github.com/darknodebros/homebrew-elesync) should
# contain this formula and auto-update the sha256 on each release via
# a GitHub Action.

class Elesync < Formula
  include Language::Python::Virtualenv

  desc "Local-first memory vault that any AI can plug into via MCP"
  homepage "https://github.com/darknodebros/EleSync"
  url "https://github.com/darknodebros/EleSync/archive/refs/tags/v1.18.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "Proprietary"
  head "https://github.com/darknodebros/EleSync.git", branch: "master"

  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "EleSync", shell_output("#{bin}/ele --version")
  end
end
