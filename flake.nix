{
  description = "clauster — self-hosted web UI for Claude Code remote-control bridges";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      version = "1.6.0";

      # The published standalone binaries, keyed by Nix system. Windows is not a Nix
      # target (use the Scoop bucket there). Checksums are auto-bumped per release by
      # packaging-bump.yml from the release SHA256SUMS.
      assets = {
        "x86_64-linux" = {
          file = "clauster-${version}-linux-x86_64";
          sha256 = "3d047f8e63307d6c1f55450a0a313af9f985972af41fb18f7edb44706e1fa40d";
        };
        "aarch64-linux" = {
          file = "clauster-${version}-linux-arm64";
          sha256 = "d01c12e3fa3f5d0807db244aa346e899cfe902e10950f9f8d15b43a1cd784cd7";
        };
        "x86_64-darwin" = {
          file = "clauster-${version}-macos-x86_64";
          sha256 = "a88f0702607ae2793e5b4c720e96bca9044761681c696d9a94441f8ac565d819";
        };
        "aarch64-darwin" = {
          file = "clauster-${version}-macos-arm64";
          sha256 = "86b2625b06e540a509e717f9cfc360de0770bc47a2a724ff49e6b9ecd48b6a11";
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
