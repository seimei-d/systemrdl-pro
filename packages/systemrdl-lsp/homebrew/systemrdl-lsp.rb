# Homebrew formula for systemrdl-lsp.
#
# Place in a tap (e.g. github.com/seimei-d/homebrew-tap/Formula/systemrdl-lsp.rb)
# and install with:
#
#   brew tap seimei-d/tap
#   brew install systemrdl-lsp
#
# When packaging a new release, update `url` to the PyPI tarball, regenerate
# `sha256` (`shasum -a 256 systemrdl_lsp-*.tar.gz`), and bump the version.
class SystemrdlLsp < Formula
  include Language::Python::Virtualenv

  desc "Language Server for SystemRDL 2.0 (diagnostics, hover, outline)"
  homepage "https://github.com/seimei-d/systemrdl-pro"
  url "https://files.pythonhosted.org/packages/source/s/systemrdl-lsp/systemrdl_lsp-0.14.7.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"

  depends_on "python@3.12"

  resource "pygls" do
    url "https://files.pythonhosted.org/packages/source/p/pygls/pygls-2.0.0.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  resource "lsprotocol" do
    url "https://files.pythonhosted.org/packages/source/l/lsprotocol/lsprotocol-2024.0.0.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  resource "systemrdl-compiler" do
    url "https://files.pythonhosted.org/packages/source/s/systemrdl-compiler/systemrdl_compiler-1.32.2.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "systemrdl-lsp", shell_output("#{bin}/systemrdl-lsp --version")
  end
end
