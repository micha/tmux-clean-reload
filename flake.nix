{
  description = "A standalone, recoverable clean reload command for tmux";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          package = pkgs.python3Packages.buildPythonApplication {
            pname = "tmux-clean-reload";
            version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
            src = self;
            pyproject = true;
            build-system = [ pkgs.python3Packages.setuptools ];

            # Prefer a user's matching tmux; provide a fallback for Nix installs.
            makeWrapperArgs = [ "--suffix" "PATH" ":" (pkgs.lib.makeBinPath [ pkgs.tmux ]) ];
            nativeCheckInputs = [ pkgs.tmux ];
            doCheck = true;
            checkPhase = ''
              runHook preCheck
              export HOME="$TMPDIR/test-home"
              mkdir -p "$HOME"
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              ${pkgs.python3.interpreter} -m unittest discover -s tests -v
              runHook postCheck
            '';
            pythonImportsCheck = [ "tmux_clean_reload" ];

            meta = {
              description = "Reset tmux prefix bindings and reload configuration with recovery snapshots";
              mainProgram = "tmux-clean-reload";
              platforms = pkgs.lib.platforms.unix;
            };
          };
        in
        {
          default = package;
          tmux-clean-reload = package;
        });

      checks = forAllSystems (system: {
        default = self.packages.${system}.default;
      });

      devShells = forAllSystems (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          default = pkgs.mkShell {
            packages = [ pkgs.python3 pkgs.python3Packages.setuptools pkgs.tmux ];
          };
        });
    };
}
