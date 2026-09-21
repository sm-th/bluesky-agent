{
  description = "Autonomous Bluesky-triggered research agent";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    llm-agents = {
      url = "github:numtide/llm-agents.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, llm-agents }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      mkAgent = pkgs: pkgs.python3.pkgs.buildPythonApplication {
        pname = "bluesky-agent";
        version = "0.1.0";
        pyproject = true;
        src = ./.;
        nativeBuildInputs = [ pkgs.python3.pkgs.setuptools ];
        propagatedBuildInputs = with pkgs.python3.pkgs; [ pillow regex ];
        doCheck = false;
      };

      # Microsandbox on Apple silicon runs an aarch64 Linux guest. OMP 18.2.4
      # is the first version this deployment accepts because its startup CLI
      # exposes --fork <session>, which Research Branch isolation requires.
      guestSystem = "aarch64-linux";
      guestPkgs = nixpkgs.legacyPackages.${guestSystem};
      guestOmp = llm-agents.packages.${guestSystem}.omp;
      guestChromium = guestPkgs.chromium;

      image =
        assert guestPkgs.lib.assertMsg
          (guestPkgs.lib.versionAtLeast guestOmp.version "18.2.4")
          "bluesky-agent requires OMP 18.2.4 or newer for startup --fork";
        let
          agent = mkAgent guestPkgs;
          entrypoint = guestPkgs.writeShellScriptBin "bluesky-agent-entrypoint" ''
            set -eu
            mkdir -p /state "$HOME"

            # OMP receives credentials only through the process environment.
            # The generated role map contains model identifiers, never keys.
            if [ -n "''${BLUESKY_AGENT_OMP_MODEL:-}" ]; then
              config_dir="$HOME/.omp/agent"
              mkdir -p "$config_dir"
              umask 077
              ${guestPkgs.python3}/bin/python3 - "$config_dir/config.yml" "$BLUESKY_AGENT_OMP_MODEL" <<'PY'
import json
import sys

path, model = sys.argv[1:]
roles = ("default", "slow", "plan", "advisor", "task", "smol", "tiny", "commit", "vision")
with open(path, "w", encoding="utf-8") as config:
    json.dump({"modelRoles": {role: model for role in roles}}, config)
    config.write("\n")
PY
            fi

            exec ${agent}/bin/bluesky-agent "$@"
          '';
        in
        guestPkgs.dockerTools.buildLayeredImage {
          name = "bluesky-agent";
          tag = "latest";
          contents = [
            agent
            guestOmp
            guestChromium
            guestPkgs.git
            guestPkgs.cacert
            guestPkgs.bash
            guestPkgs.coreutils
            guestPkgs.fontconfig
            guestPkgs.noto-fonts
            guestPkgs.dockerTools.fakeNss
            entrypoint
          ];
          extraCommands = ''
            mkdir -p state
          '';
          config = {
            Entrypoint = [ "${entrypoint}/bin/bluesky-agent-entrypoint" ];
            Cmd = [ "once" ];
            WorkingDir = "/state";
            Env = [
              "HOME=/state/home"
              "PYTHONUNBUFFERED=1"
              "SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
              "GIT_SSL_CAINFO=/etc/ssl/certs/ca-bundle.crt"
              "BLUESKY_AGENT_STATE_DIR=/state"
              "BLUESKY_AGENT_CHROMIUM_BIN=${guestPkgs.lib.getExe guestChromium}"
              "PUPPETEER_EXECUTABLE_PATH=${guestPkgs.lib.getExe guestChromium}"
            ];
          };
        };
    in
    {
      packages = forAllSystems (pkgs:
        let
          agent = mkAgent pkgs;
          omp = llm-agents.packages.${pkgs.stdenv.hostPlatform.system}.omp;

          debug = pkgs.writeShellApplication {
            name = "bluesky-agent-debug";
            runtimeInputs = [ agent omp pkgs.secretspec ];
            text = ''
              if [ "$#" -eq 0 ]; then set -- doctor; fi
              case "$1" in
                activate|once|run|doctor|show) ;;
                *)
                  echo "usage: nix run .#debug -- activate|once|run|doctor|show" >&2
                  exit 2
                  ;;
              esac

              config="''${BLUESKY_AGENT_CONFIG:-$PWD/bluesky_agent.toml}"
              if [ -f "$config" ]; then
                export BLUESKY_AGENT_CONFIG="$config"
              fi
              export PYTHONDEVMODE=1
              export PYTHONWARNINGS="''${PYTHONWARNINGS:-default}"
              exec secretspec run --provider dotenv \
                --reason "Run bluesky-agent in debug mode" -- \
                bluesky-agent "$@"
            '';
          };

          sandbox = pkgs.writeShellApplication {
            name = "bluesky-agent-sandbox";
            runtimeInputs = [ pkgs.coreutils pkgs.gzip pkgs.nix pkgs.python3 pkgs.secretspec ];
            text = ''
              if [ "$#" -eq 0 ]; then set -- doctor; fi
              case "$1" in
                activate|once|run|doctor|show) ;;
                *)
                  echo "usage: nix run .#sandbox -- activate|once|run|doctor|show" >&2
                  exit 2
                  ;;
              esac

              msb=$(command -v msb || true)
              for candidate in "$HOME/.local/bin/msb" "$HOME/.microsandbox/bin/msb"; do
                [ -n "$msb" ] && break
                [ -x "$candidate" ] && msb="$candidate"
              done
              if [ -z "$msb" ]; then
                echo "microsandbox (msb) was not found in PATH, ~/.local/bin, or ~/.microsandbox/bin" >&2
                exit 127
              fi

              # Resolve the declared credentials exactly once on the host. The
              # guest receives only the explicit whitelist assembled below.
              if [ -z "''${_BLUESKY_AGENT_SECRETSPEC:-}" ]; then
                exec secretspec run --provider dotenv \
                  --reason "Run bluesky-agent in Microsandbox" -- \
                  env _BLUESKY_AGENT_SECRETSPEC=1 "$0" "$@"
              fi

              echo "building aarch64-linux OCI image..." >&2
              image_tar=$(nix build "${self}#image" --no-link --print-out-paths)
              gzip -dc "$image_tar" | "$msb" load --quiet --tag bluesky-agent:latest

              config="''${BLUESKY_AGENT_CONFIG:-$PWD/bluesky_agent.toml}"
              secret_args=()
              add_secret() {
                name="$1"
                hosts="$2"
                if [ -n "''${!name:-}" ]; then
                  secret_args+=(--secret "$name@$hosts")
                fi
              }
              url_host() {
                ${pkgs.python3}/bin/python3 - "$1" <<'PY'
import sys
from urllib.parse import urlsplit

host = urlsplit(sys.argv[1]).hostname
if not host:
    raise SystemExit("configured service URL has no host")
print(host)
PY
              }

              bluesky_service="''${BLUESKY_AGENT_BLUESKY_SERVICE:-https://bsky.social}"
              if [ -f "$config" ] && [ -z "''${BLUESKY_AGENT_BLUESKY_SERVICE:-}" ]; then
                bluesky_service=$(
                  BLUESKY_AGENT_LAUNCH_CONFIG="$config" ${pkgs.python3}/bin/python3 <<'PY'
import os
import tomllib

with open(os.environ["BLUESKY_AGENT_LAUNCH_CONFIG"], "rb") as source:
    values = tomllib.load(source)
if "bluesky_agent" in values:
    values = values["bluesky_agent"]
print(values.get("bluesky_service", "https://bsky.social"))
PY
                )
              fi
              add_secret BLUESKY_AGENT_APP_PASSWORD "$(url_host "$bluesky_service")"
              add_secret BLUESKY_AGENT_GITHUB_TOKEN github.com
              add_secret OPENAI_API_KEY "$(url_host "''${OPENAI_BASE_URL:-https://api.openai.com/v1}")"
              add_secret ANTHROPIC_API_KEY api.anthropic.com
              add_secret GEMINI_API_KEY generativelanguage.googleapis.com
              add_secret OPENROUTER_API_KEY openrouter.ai
              add_secret GROQ_API_KEY api.groq.com
              add_secret XAI_API_KEY api.x.ai
              add_secret MISTRAL_API_KEY api.mistral.ai
              add_secret DEEPSEEK_API_KEY api.deepseek.com
              add_secret EXA_API_KEY api.exa.ai
              add_secret BRAVE_API_KEY api.search.brave.com
              add_secret TAVILY_API_KEY api.tavily.com

              env_args=()
              structural_names=(
                BLUESKY_AGENT_OPERATOR_HANDLE
                BLUESKY_AGENT_OPERATOR_DID
                BLUESKY_AGENT_AGENT_HANDLE
                BLUESKY_AGENT_BLUESKY_SERVICE
                BLUESKY_AGENT_BLUESKY_PUBLIC_API
                BLUESKY_AGENT_WIKI_REPO_URL
                BLUESKY_AGENT_WIKI_SITE_URL
                BLUESKY_AGENT_POLL_INTERVAL
                BLUESKY_AGENT_OMP_MODEL
                BLUESKY_AGENT_OMP_TIMEOUT
                BLUESKY_AGENT_INTENT_MODEL
                BLUESKY_AGENT_INTENT_TIMEOUT
                BLUESKY_AGENT_INTENT_CONFIDENCE_THRESHOLD
                BLUESKY_AGENT_WIKI_READY_TIMEOUT
                BLUESKY_AGENT_WIKI_READY_INTERVAL
                BLUESKY_AGENT_GIT_USER_NAME
                BLUESKY_AGENT_GIT_USER_EMAIL
                OPENAI_BASE_URL
              )
              for name in "''${structural_names[@]}"; do
                if [ -n "''${!name:-}" ]; then
                  env_args+=(--env "$name=''${!name}")
                fi
              done

              # Convert only known non-secret TOML fields to guest environment
              # variables. Environment values take precedence over the file.
              config="''${BLUESKY_AGENT_CONFIG:-$PWD/bluesky_agent.toml}"
              if [ -f "$config" ]; then
                while IFS= read -r -d "" assignment; do
                  env_args+=(--env "$assignment")
                done < <(
                  BLUESKY_AGENT_LAUNCH_CONFIG="$config" ${pkgs.python3}/bin/python3 <<'PY'
import os
import sys
import tomllib

fields = {
    "operator_handle",
    "operator_did",
    "agent_handle",
    "bluesky_service",
    "bluesky_public_api",
    "wiki_repo_url",
    "wiki_site_url",
    "poll_interval",
    "omp_model",
    "omp_timeout",
    "intent_model",
    "intent_timeout",
    "intent_confidence_threshold",
    "wiki_ready_timeout",
    "wiki_ready_interval",
    "git_user_name",
    "git_user_email",
}
with open(os.environ["BLUESKY_AGENT_LAUNCH_CONFIG"], "rb") as source:
    values = tomllib.load(source)
if "bluesky_agent" in values:
    values = values["bluesky_agent"]
for field in sorted(fields):
    env_name = f"BLUESKY_AGENT_{field.upper()}"
    if env_name in os.environ or field not in values:
        continue
    value = str(values[field])
    if "\0" in value:
        raise SystemExit(f"NUL byte in configuration field: {field}")
    sys.stdout.buffer.write(f"{env_name}={value}".encode() + b"\0")
PY
                )
              fi

              # Guest-local paths cannot be overridden by host configuration.
              env_args+=(
                --env BLUESKY_AGENT_STATE_DIR=/state
                --env BLUESKY_AGENT_CHROMIUM_BIN=${guestPkgs.lib.getExe guestChromium}
              )

              state_dir="$PWD/.state"
              mkdir -p "$state_dir"
              exec "$msb" run --replace --name bluesky-agent \
                --cpus "''${BLUESKY_AGENT_SANDBOX_CPUS:-4}" \
                --memory "''${BLUESKY_AGENT_SANDBOX_MEMORY:-8G}" \
                --mount-dir "$state_dir:/state:rw" \
                --net public \
                --on-secret-violation block-and-terminate \
                "''${secret_args[@]}" \
                "''${env_args[@]}" \
                bluesky-agent:latest -- "$@"
            '';
          };
        in
        {
          default = agent;
          inherit image debug sandbox;
        });

      apps = forAllSystems (pkgs:
        let
          system = pkgs.stdenv.hostPlatform.system;
        in
        {
          default = {
            type = "app";
            program = "${self.packages.${system}.default}/bin/bluesky-agent";
          };
          debug = {
            type = "app";
            program = "${self.packages.${system}.debug}/bin/bluesky-agent-debug";
          };
          sandbox = {
            type = "app";
            program = "${self.packages.${system}.sandbox}/bin/bluesky-agent-sandbox";
          };
        });

      devShells = forAllSystems (pkgs:
        let
          system = pkgs.stdenv.hostPlatform.system;
        in
        {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (python: [ python.pillow python.pytest python.regex ]))
              llm-agents.packages.${system}.omp
              pkgs.git
              pkgs.secretspec
            ];
            shellHook = ''
              echo "bluesky-agent: nix run .#debug -- doctor"
              echo "sandbox:      nix run .#sandbox -- activate|once|run|doctor|show"
            '';
          };
        });
    };
}
