{
  description = "clauster — self-hosted web UI for Claude Code remote-control bridges";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      version = "1.2.0";

      # The published standalone binaries, keyed by Nix system. Windows is not a Nix
      # target (use the Scoop bucket there). Checksums are auto-bumped per release by
      # packaging-bump.yml from the release SHA256SUMS.
      assets = {
        "x86_64-linux" = {
          file = "clauster-${version}-linux-x86_64";
          sha256 = "4ee7df27f19d09ae1dff225aa63a6b4d42459ec048d4c0cd118d17b1f39f831e";
        };
        "aarch64-linux" = {
          file = "clauster-${version}-linux-arm64";
          sha256 = "3c7fec2deb17cecf9a0add4a6422d8e2a5bf4047535b1b67292fe2af4018c545";
        };
        "x86_64-darwin" = {
          file = "clauster-${version}-macos-x86_64";
          sha256 = "5402b179c4ad5a0c644e09fe015e1c4705f8d8e958882dd204279c8100f4f404";
        };
        "aarch64-darwin" = {
          file = "clauster-${version}-macos-arm64";
          sha256 = "e1baae8d272b76d6378e7e5a8e14846adf34d403210cc62223ccdae903f994fb";
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
