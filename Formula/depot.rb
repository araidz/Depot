class Depot < Formula
  desc "Lightweight macOS installer & firmware downloader TUI"
  homepage "https://github.com/araidz/Depot"
  url "https://github.com/araidz/Depot/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "PLACEHOLDER"
  license "MIT"

  depends_on "aria2"
  depends_on "python@3.14"

  def install
    libexec.install "depot"
    (bin/"depot").write <<~SH
      #!/bin/sh
      export PYTHONPATH="#{libexec}:$PYTHONPATH"
      exec "#{formula_opt_bin("python@3.14")}/python3.14" -m depot "$@"
    SH
  end

  test do
    assert_match "macOS installer", shell_output("#{bin}/depot --help")
  end
end
