"""Check a built community bundle from clean DSH homes and prepare its Discover index."""
import argparse
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPOSITORY = "https://github.com/kaust-ark/ARK"


def retain_versions(market, previous):
    """Keep prior compatible installs available on the profile's subscribed index."""
    entry, = market["plugins"]
    old, = previous["plugins"]
    if previous["format"] != 1 or old["name"] != entry["name"]:
        raise ValueError("Expected the previous Idea2Paper Discover index")
    versions = {row["version"]: row for row in entry["versions"]}
    for row in old["versions"]:
        source = row["source"]
        if (source.get("url") != REPOSITORY or source.get("type") != "git"
                or not re.fullmatch(r"[0-9a-f]{40}", str(source.get("commit", "")))):
            raise ValueError("Expected a pinned Idea2Paper Git release")
        number = row["version"]
        if number in versions and versions[number] != row:
            raise ValueError(f"Previously published version {number} changed")
        versions[number] = row
    entry["versions"] = list(versions.values())


def prepare(bundle, target, previous_index=None):
    target.mkdir(parents=True, exist_ok=False)
    with tarfile.open(bundle, "r:gz") as tar:
        for member in tar:
            key = Path(member.name)
            if not key.parts or key.parts[0] != "package" or any(p.startswith(".") for p in key.parts):
                raise ValueError(f"Invalid npm package entry: {key}")
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f"Expected a regular file: {key}")
            dest = target.joinpath(*key.parts[1:])
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("xb") as out, tar.extractfile(member) as src:
                shutil.copyfileobj(src, out)
    package = json.loads((target / "package.json").read_text())
    version = package["version"]
    if package["name"] != "idea2paper-dsh" or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?", version):
        raise ValueError("Unexpected community package name or version")
    manifest = json.loads((target / "companion/manifest.json").read_text())
    wheel, = (target / "companion").glob("*.whl")
    if manifest != {"wheel":wheel.name, "sha256":hashlib.sha256(wheel.read_bytes()).hexdigest()}:
        raise ValueError("Packaged companion digest mismatch")

    tag = "dsh-bundle-" + version
    date = int(os.environ.get("SOURCE_DATE_EPOCH") or time.time())
    env = dict(os.environ, GIT_AUTHOR_NAME="Idea2Paper release", GIT_COMMITTER_NAME="Idea2Paper release",
               GIT_AUTHOR_EMAIL="contact@idea2paper.org", GIT_COMMITTER_EMAIL="contact@idea2paper.org",
               GIT_AUTHOR_DATE=f"{date} +0000", GIT_COMMITTER_DATE=f"{date} +0000")

    def git(*args):
        return subprocess.check_output(["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
                                        "-c", "core.hooksPath=/dev/null", *args], cwd=target, env=env, text=True).strip()

    git("init", "-q")
    git("add", "-f", ".")
    git("commit", "-qm", f"Package Idea2Paper {version}")
    commit = git("rev-parse", "HEAD")
    market = {"format":1, "title":"Idea2Paper", "plugins":[{
        "name":package["name"], "title":"Idea2Paper",
        "description":{"en":"Run a research project with experiments, independent review and a checked paper.",
                       "zh":"完成研究实验、独立评审和论文交付。"},
        "author":"Idea2Paper Team", "homepage":REPOSITORY,
        "repository":{"url":REPOSITORY, "directory":"integrations/dsh"},
        "versions":[{"version":version, "publishTimestamp":date, "engines":{"dsh":package["engines"]["dsh"]},
                     "source":{"type":"git", "url":REPOSITORY, "commit":commit}}]}]}
    if previous_index is not None:
        retain_versions(market, json.loads(previous_index.read_text()))
    (target / "index.json").write_text(json.dumps(market, ensure_ascii=False, indent=2) + "\n")
    git("add", "index.json")
    git("commit", "-qm", f"Discover index for {version}")
    git("tag", tag)
    return package, tag, commit


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def verify_editorial(profile, dsh, scratch, *, python=None):
    """Run the native editorial replay against this installed package and its bundled wheel."""
    profile = Path(profile).resolve(strict=True)
    package = profile / "node_modules/idea2paper-dsh"
    companion = package / "companion"
    wheel, = companion.glob("*.whl")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if json.loads((companion / "manifest.json").read_text()) != {"wheel": wheel.name, "sha256": digest}:
        raise ValueError("Installed profile's editorial companion digest mismatch")
    scratch = Path(scratch).resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    runtime = scratch / "companion"
    runtime.mkdir()
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(runtime)
    scenario = Path(__file__).resolve().parents[2] / "tests/integration/test_dsh_editorial_loop.py"
    # -I ignores inherited PYTHONPATH and the working directory. Only the test protocol lives
    # in the checkout. The profile's verified wheel takes precedence over any installed ark.
    launch = ("import runpy, sys; sys.path.insert(0, sys.argv[-1]); "
              "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')")
    env = {"PATH": os.environ["PATH"], "HOME": str(scratch), "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    if "PKG_NATIVE_CACHE_PATH" in os.environ:
        env["PKG_NATIVE_CACHE_PATH"] = os.environ["PKG_NATIVE_CACHE_PATH"]
    subprocess.run([python or sys.executable, "-I", "-c", launch, str(scenario), str(profile),
                    str(dsh), str(scratch), str(runtime)], cwd=scratch, env=env, check=True, timeout=360)
    print(f"Verified editorial rejection, revision and independent acceptance with companion {digest[:12]}.",
          flush=True)


def verify(bundle, target, package, commit, scratch):
    dsh = shutil.which("dsh")
    if not dsh:
        raise SystemExit(f"Install a supported DSH ({package['engines']['dsh']}) and put dsh on PATH.")
    env = {"PATH":os.environ["PATH"], "HOME":str(scratch), "DSH_HOME":str(scratch / "dsh"),
           "DSH_TELEMETRY_DISABLED":"1", "PIP_NO_INPUT":"1", "PYTHONNOUSERSITE":"1"}
    for key in ("PIP_CACHE_DIR", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_CERT", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        if key in os.environ:
            env[key] = os.environ[key]
    def run(*args, timeout=120):
        result = subprocess.run([dsh, *args], cwd=scratch, env=env, timeout=timeout, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError(result.stdout[-3000:])
        return result.stdout

    if run("--version").strip() not in package["engines"]["dsh"].split(" || "):
        raise ValueError(f"Use DSH {package['engines']['dsh']}")
    public = scratch / "www"
    public.mkdir()
    shutil.copyfile(target / "index.json", public / "index.json")
    shutil.copyfile(bundle, public / bundle.name)
    handler = functools.partial(QuietHandler, directory=str(public))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            run("plugin", "--profile", "idea2paper", "add", url + "/" + bundle.name)
            help_text = run("--profile", "idea2paper", "--help")
            command_list = help_text.replace(" [options]", "")
            for command in ("run <project>", "resume <project>", "stop <project>", "status <project>"):
                if command not in command_list:
                    raise ValueError(f"Missing native package command: {command}\n{help_text}")
            print("Verified remote tarball installation and the native commands.", flush=True)
            # Read a stopped workspace via the packaged, isolated Python companion.
            project = scratch / "study"
            state = project / "auto_research/state"
            state.mkdir(parents=True)
            (state / "idea.md").write_text("Measure comparisons on sorted inputs.")
            (state / "native_project.json").write_text(json.dumps({"version":1,"state":"stopped"}))
            (project / "workflow.json").write_text('{"version":3}')
            output = run("--profile", "idea2paper", "status", str(project), timeout=900)
            if not any(line.startswith("{") and json.loads(line).get("state") == "stopped" for line in output.splitlines()):
                raise ValueError("The packaged companion could not read the stopped project")

            print("Verified the bundled Python companion outside the source checkout.", flush=True)
            profile = Path(env["DSH_HOME"]) / "profiles/idea2paper"
            installed = profile / "node_modules/idea2paper-dsh/companion/manifest.json"
            expected = json.loads((target / "companion/manifest.json").read_text())
            if json.loads(installed.read_text()) != expected:
                raise ValueError("Installed profile's companion does not match the release tarball")
            # status bootstrapped the full runtime dependencies in this clean HOME.
            caches = (scratch / ".cache/idea2paper/companions").glob("*/.ready")
            python, = (entry.parent / "bin/python" for entry in caches if entry.read_text() == expected["sha256"])
            verify_editorial(profile, dsh, scratch / "editorial", python=python)
            # Read the index using the same market service as the Discover UI.
            entry_url = url + "/index.json"
            spec = "git+" + REPOSITORY + ".git#" + commit
            probe = scratch / "probe.mjs"
            probe.write_text(f"""export const inject = ['pluginMarket','webStartup','appReady']
export function apply(ctx) {{
  ctx.get('appReady').onReady(async () => {{
    try {{
      const entry = await ctx.get('pluginMarket').entry({json.dumps(entry_url)}, 'idea2paper-dsh')
      if (entry?.defaultVersion !== {json.dumps(package['version'])} ||
          entry?.versions[0]?.install.spec !== {json.dumps(spec)}) throw Error('Unexpected Discover entry')
      console.log('VERIFIED_MARKET_TARGET ' + entry.versions[0].install.spec)
      ctx.get('appExit')(0)
    }} catch (e) {{ console.error(e); ctx.get('appExit')(1) }}
  }})
}}
""")
            patch = scratch / "market.yml"
            patch.write_text(f"- insert:\n    - id: plugin-market\n      name: '@deepseek-ai/dsh-plugin-market'\n"
                             f"      config:\n        indexes: ['{entry_url}']\n    - id: fixture\n      name: '{probe}'\n")
            web = run("--profile","web","--patch",str(patch),"--host","127.0.0.1","--port","0","--no-open")
            if "VERIFIED_MARKET_TARGET " + spec not in web:
                raise RuntimeError("DSH Web did not confirm the pinned Discover entry")
            # Exercise pnpm's Git installer on the exact target commit before it
            # is public. Git resolves this fixture hostname to the staging repository.
            mirror = "https://idea2paper.invalid/bundle.git"
            env.update(DSH_HOME=str(scratch / "git-profile"), GIT_CONFIG_COUNT="1",
                       GIT_CONFIG_KEY_0="url." + target.as_uri() + ".insteadOf",
                       GIT_CONFIG_VALUE_0=mirror, GIT_TERMINAL_PROMPT="0")
            run("plugin", "--profile", "idea2paper", "add", "git+" + mirror + "#" + commit)
            if "stop <project>" not in run("--profile", "idea2paper", "--help"):
                raise RuntimeError("Pinned Git installation did not load the native commands")
        finally:
            server.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--prepare", type=Path, help="retain the distribution repository in a new directory")
    parser.add_argument("--previous-index", type=Path, help="retain releases from the subscribed Discover index")
    opts = parser.parse_args()
    bundle = opts.bundle.resolve(strict=True)
    destination = opts.prepare.absolute() if opts.prepare else None
    if destination:
        if os.path.lexists(destination):
            parser.error("--prepare requires a new directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="idea2paper-distribution-") as tmp:
        scratch = Path(tmp)
        # Publish the local staging directory only after every verification passed.
        with tempfile.TemporaryDirectory(dir=destination.parent if destination else scratch) as staging:
            target = Path(staging) / "package"
            package, tag, commit = prepare(bundle, target, opts.previous_index)
            verify(bundle, target, package, commit, scratch)
            print(f"Verified packaged CLI, Python companion, DSH Web and Discover commit {commit}.")
            if destination:
                target.rename(destination)
                print(f"Prepared {tag} in {destination}.")
                print(f"Index after publication: https://raw.githubusercontent.com/kaust-ark/ARK/{tag}/index.json")



if __name__ == "__main__":
    main()
