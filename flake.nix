{
  description = "clauster — self-hosted web UI for Claude Code remote-control bridges";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      version = "1.0.2";

      # The published standalone binaries, keyed by Nix system. Windows is not a Nix
      # target (use the Scoop bucket there). Checksums are auto-bumped per release by
      # packaging-bump.yml from the release SHA256SUMS.
      assets = {
        "x86_64-linux" = {
          file = "clauster-${version}-linux-x86_64";
          sha256 = "8f01558ad0375344d968fba36fe84a659e0583a2554f67b99e56a7c70ff472c1";
        };
        "aarch64-linux" = {
          file = "clauster-${version}-linux-arm64";
          sha256 = "f4f984f8a90968c261c18ceaed2ec07d0af38b1df9f8e5727d1c142a33a7c1b7";
        };
        "x86_64-darwin" = {
          file = "clauster-${version}-macos-x86_64";
          sha256 = "e7655cf55c62c1eafffc905b837ef1c51d94e775e6096b0020bba0726bbe3e43";
        };
        "aarch64-darwin" = {
          file = "clauster-${version}-macos-arm64";
          sha256 = "40678431c9f7980631f90eed24a7f7a5293b94e6d629ba645b9f87efb6bbaf63";
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
