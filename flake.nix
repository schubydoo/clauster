{
  description = "clauster — self-hosted web UI for Claude Code remote-control bridges";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      version = "1.4.0";

      # The published standalone binaries, keyed by Nix system. Windows is not a Nix
      # target (use the Scoop bucket there). Checksums are auto-bumped per release by
      # packaging-bump.yml from the release SHA256SUMS.
      assets = {
        "x86_64-linux" = {
          file = "clauster-${version}-linux-x86_64";
          sha256 = "040de0b97b63e07bc14a8d0dbaa8374a7b247cc56d1ffe88afc5e53bf595b55e";
        };
        "aarch64-linux" = {
          file = "clauster-${version}-linux-arm64";
          sha256 = "6c568291533ad94fa2e5f52322611e40af9e289cbbd4a7a7df8cfe1d8ee160b2";
        };
        "x86_64-darwin" = {
          file = "clauster-${version}-macos-x86_64";
          sha256 = "4e3f403fc75dd696eea4c0a4f264c66b18b42e3caeb30ce3b8396fbc57fb98d1";
        };
        "aarch64-darwin" = {
          file = "clauster-${version}-macos-arm64";
          sha256 = "f07cbd1f85e1f0a251057cf5cefc92aca1994263bf98ac87c4a893adc016f80e";
        };
      };

      systems = builtins.attrNames assets;
      forAllSystems = nixpkgs.lib.genAttrs systems;

      packageFor =
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          asset = assets.${system};
        in
        pkgs.stdenvNoCC.mkDerivation {
          pname = "clauster";
          inherit version;

          src = pkgs.fetchurl {
            url = "https://github.com/schubydoo/clauster/releases/download/v${version}/${asset.file}";
            inherit (asset) sha256;
          };

          dontUnpack = true;

          # The Linux binary is a PyInstaller one-file ELF; patch its loader/RPATH to
          # the Nix store. Darwin needs no patching.
          nativeBuildInputs = pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.autoPatchelfHook ];
          buildInputs = pkgs.lib.optionals pkgs.stdenv.isLinux [
            (pkgs.lib.getLib pkgs.stdenv.cc.cc)
            pkgs.zlib
          ];

          installPhase = ''
            runHook preInstall
            install -Dm755 "$src" "$out/bin/clauster"
            runHook postInstall
          '';

          meta = with pkgs.lib; {
            description = "Self-hosted web UI for spawning and managing Claude Code remote-control bridges";
            homepage = "https://github.com/schubydoo/clauster";
            license = licenses.asl20;
            platforms = systems;
            mainProgram = "clauster";
            sourceProvenance = [ sourceTypes.binaryNativeCode ];
          };
        };
    in
    {
      packages = forAllSystems (system: rec {
        clauster = packageFor system;
        default = clauster;
      });

      apps = forAllSystems (system: rec {
        clauster = {
          type = "app";
          program = "${self.packages.${system}.clauster}/bin/clauster";
        };
        default = clauster;
      });
    };
}
