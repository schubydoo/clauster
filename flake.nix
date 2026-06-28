{
  description = "clauster — self-hosted web UI for Claude Code remote-control bridges";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      version = "0.12.8";

      # The published standalone binaries, keyed by Nix system. Windows is not a Nix
      # target (use the Scoop bucket there). Checksums are auto-bumped per release by
      # packaging-bump.yml from the release SHA256SUMS.
      assets = {
        "x86_64-linux" = {
          file = "clauster-${version}-linux-x86_64";
          sha256 = "5a78b82918d1b52a4352cd239d91841e1b84367fdd450c3525d618cf2a6ff125";
        };
        "aarch64-linux" = {
          file = "clauster-${version}-linux-arm64";
          sha256 = "ee58441b7510c803cdd6703f0cc4c9c5894efa90909778041f54372216327afc";
        };
        "x86_64-darwin" = {
          file = "clauster-${version}-macos-x86_64";
          sha256 = "2ea82bbc8c5474a55779d333057e2cd37fbaa142253486c43f76d0805adaee65";
        };
        "aarch64-darwin" = {
          file = "clauster-${version}-macos-arm64";
          sha256 = "0ef1d41b88699952d4713c1c593183aef68f66dfc4bdc82793f6f4ba8f8ed675";
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
