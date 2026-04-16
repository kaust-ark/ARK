import os
import json
import shutil
import subprocess
import httpx
import asyncio
from pathlib import Path
from typing import Dict, Any

def verify_anthropic(api_key: str) -> Dict[str, Any]:
    """Verify Anthropic API key with a minimal request."""
    if not api_key:
        return {"ok": False, "msg": "No API key provided"}
    
    try:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        data = {
            "model": "claude-haiku-4-5",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "Hello"}]
        }
        
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, headers=headers, json=data)
            if resp.status_code == 200:
                return {"ok": True, "msg": "Functional"}
            else:
                try:
                    err = resp.json().get("error", {}).get("message", resp.text)
                except:
                    err = resp.text
                return {"ok": False, "msg": f"Failed: {err}"}
    except Exception as e:
        return {"ok": False, "msg": f"Error: {str(e)}"}

def verify_gemini(api_key: str) -> Dict[str, Any]:
    """Verify Gemini API key with a minimal request."""
    if not api_key:
        return {"ok": False, "msg": "No API key provided"}
    
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-preview:generateContent?key={api_key}"
        headers = {"content-type": "application/json"}
        data = {
            "contents": [{"parts": [{"text": "Hello"}]}],
            "generationConfig": {"maxOutputTokens": 1}
        }
        
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, headers=headers, json=data)
            if resp.status_code == 200:
                return {"ok": True, "msg": "Functional"}
            else:
                try:
                    err = resp.json().get("error", {}).get("message", resp.text)
                except:
                    err = resp.text
                return {"ok": False, "msg": f"Failed: {err}"}
    except Exception as e:
        return {"ok": False, "msg": f"Error: {str(e)}"}

def verify_openai(api_key: str) -> Dict[str, Any]:
    """Verify OpenAI API key with a minimal request."""
    if not api_key:
        return {"ok": False, "msg": "No API key provided"}
    
    try:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": "gpt-5-nano",
            "max_completion_tokens": 10,
            "messages": [{"role": "user", "content": "Hello"}]
        }
        
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, headers=headers, json=data)
            if resp.status_code == 200:
                return {"ok": True, "msg": "Functional"}
            else:
                try:
                    err = resp.json().get("error", {}).get("message", resp.text)
                except:
                    err = resp.text
                return {"ok": False, "msg": f"Failed: {err}"}
    except Exception as e:
        return {"ok": False, "msg": f"Error: {str(e)}"}

def verify_claude_cli(user_id: str, projects_root: Path, keys: Dict[str, str]) -> Dict[str, Any]:
    """Verify Claude CLI headless setup in a user-specific temporary directory."""
    token = keys.get("claude_oauth_token")
    if not token:
        return {"ok": False, "msg": "No OAuth Token provided"}
    
    # 1. Prepare user-specific verification directory
    # Mirroring actual project usage in {projects_root}/{user_id}/.verify_claude
    verify_dir = projects_root / str(user_id) / ".verify_claude"
    if verify_dir.exists():
        shutil.rmtree(verify_dir)
    verify_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        # Detect version for .claude.json
        from ark.webapp.jobs import get_claude_version
        version = get_claude_version()
        
        # 2. Provision session file
        config = {
            "hasCompletedOnboarding": True,
            "lastOnboardingVersion": version
        }
        (verify_dir / ".claude.json").write_text(json.dumps(config))
        
        # 3. Run Claude CLI with restricted environment
        # Strip CLAUDECODE to prevent nested-session issues
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        # systemd's bare PATH doesn't include ~/.nvm/.../bin where the claude
        # CLI lives, so resolve it explicitly and enrich PATH so claude itself
        # can find node, etc.
        from ark.webapp.jobs import find_claude_binary, build_subprocess_path
        env["PATH"] = build_subprocess_path()
        env["PYTHONNOUSERSITE"] = "1"
        # Isolate HOME so this verification can't pick up any other user's
        # ~/.claude state and so it leaves no trace in the real $HOME.
        env["HOME"] = str(verify_dir)
        env["XDG_CONFIG_HOME"] = str(verify_dir / ".config")
        claude_bin = find_claude_binary()
        if not claude_bin:
            return {"ok": False, "msg": "claude CLI not found on server (checked PATH, ~/.nvm, ~/.local/bin)"}

        # Use a model that's cheap and fast for testing (Haiku if available, else default)
        cmd = [
            claude_bin, "-p", "Say 'Functional' in one word",
            "--output-format", "text"
        ]


        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(verify_dir),
            env=env,
            timeout=30.0
        )
        
        if result.returncode == 0:
            return {"ok": True, "msg": "Functional"}
        else:
            err = result.stderr.strip() or result.stdout.strip()
            # Clean up potentially long CLI errors
            err = err[:200]
            return {"ok": False, "msg": f"CLI Error: {err}"}
            
    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": "Claude CLI timed out (30s)"}
    except Exception as e:
        return {"ok": False, "msg": f"Error: {str(e)}"}
    finally:
        # 4. Cleanup
        if verify_dir.exists():
            try:
                shutil.rmtree(verify_dir)
            except:
                pass

def verify_gemini_cli(user_id: str, projects_root: Path, keys: Dict[str, str]) -> Dict[str, Any]:
    """Verify Gemini CLI headless setup in a user-specific temporary directory."""
    api_key = keys.get("gemini") or keys.get("gemini_api_key")
    if not api_key:
        return {"ok": False, "msg": "No Gemini API Key provided"}
    
    verify_dir = projects_root / str(user_id) / ".verify_gemini"
    if verify_dir.exists():
        shutil.rmtree(verify_dir)
    verify_dir.mkdir(parents=True, exist_ok=True)
    gemini_dir = verify_dir / ".gemini"
    gemini_dir.mkdir(exist_ok=True)
    (gemini_dir / "projects.json").write_text('{"projects":{}}')

    try:
        from ark.webapp.jobs import find_gemini_binary, build_subprocess_path
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["GEMINI_API_KEY"] = api_key
        env["PATH"] = build_subprocess_path()
        env["PYTHONNOUSERSITE"] = "1"
        env["HOME"] = str(verify_dir)
        env["XDG_CONFIG_HOME"] = str(verify_dir / ".config")
        env["TMPDIR"] = str(verify_dir)
        
        gemini_bin = find_gemini_binary()
        if not gemini_bin:
            return {"ok": False, "msg": "gemini CLI not found on server"}

        cmd = [
            gemini_bin, "-p", "Say 'Functional' in one word",
            "-o", "text"
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(verify_dir),
            env=env,
            timeout=90.0
        )

        if result.returncode == 0:
            return {"ok": True, "msg": "Functional"}
        else:
            err = result.stderr.strip() or result.stdout.strip()
            err = err[:200]
            return {"ok": False, "msg": f"CLI Error: {err}"}

    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": "Gemini CLI timed out (90s)"}
    except Exception as e:
        return {"ok": False, "msg": f"Error: {str(e)}"}
    finally:
        if verify_dir.exists():
            try:
                shutil.rmtree(verify_dir)
            except:
                pass

def verify_gemini_oauth(user_id: str, projects_root: Path, keys: Dict[str, str]) -> Dict[str, Any]:
    """Verify Gemini CLI headless setup using OAuth JSON."""
    oauth_json = keys.get("gemini_oauth_json")
    if not oauth_json:
        return {"ok": False, "msg": "No Gemini OAuth JSON provided"}
    
    try:
        json.loads(oauth_json)
    except Exception as e:
        return {"ok": False, "msg": f"Invalid JSON: {str(e)}"}

    verify_dir = projects_root / str(user_id) / ".verify_gemini_oauth"
    if verify_dir.exists():
        shutil.rmtree(verify_dir)
    verify_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        from ark.webapp.jobs import find_gemini_binary, build_subprocess_path, provision_gemini_session
        env = os.environ.copy()
        env["PATH"] = build_subprocess_path()
        env["PYTHONNOUSERSITE"] = "1"
        env["HOME"] = str(verify_dir)
        env["XDG_CONFIG_HOME"] = str(verify_dir / ".config")
        env["TMPDIR"] = str(verify_dir)

        # Provision the oauth file
        provision_gemini_session(verify_dir, keys)
        
        gemini_bin = find_gemini_binary()
        if not gemini_bin:
            return {"ok": False, "msg": "gemini CLI not found on server"}

        # Running a simple command to verify auth
        cmd = [gemini_bin, "-p", "Say 'Functional' in one word", "-o", "text"]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(verify_dir),
            env=env,
            timeout=90.0
        )

        if result.returncode == 0:
            return {"ok": True, "msg": "Functional (OAuth)"}
        else:
            err = result.stderr.strip() or result.stdout.strip()
            err = err[:200]
            return {"ok": False, "msg": f"CLI Error: {err}"}

    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": "Gemini CLI timed out (90s)"}
    except Exception as e:
        return {"ok": False, "msg": f"Error: {str(e)}"}
    finally:
        if verify_dir.exists():
            try:
                shutil.rmtree(verify_dir)
            except:
                pass

def run_verification_suite(user_id: str, projects_root: Path, keys: Dict[str, str]) -> Dict[str, Any]:
    """Run all relevant verifications in parallel."""
    results = {}
    
    # We can use ThreadPoolExecutor for real parallelization if needed, 
    # but for simplicity and small number of keys, sequential or simple async is fine.
    
    # 1. LLM API Keys
    if keys.get("gemini"):
        results["gemini"] = verify_gemini(keys["gemini"])
    
    if keys.get("anthropic"):
        results["anthropic"] = verify_anthropic(keys["anthropic"])
        
    if keys.get("openai"):
        results["openai"] = verify_openai(keys["openai"])

        
    # 2. Claude CLI
    if keys.get("claude_oauth_token"):
        results["claude_token"] = verify_claude_cli(user_id, projects_root, keys)
        
    # 3. Gemini CLI
    if keys.get("gemini_oauth_json"):
        results["gemini_oauth"] = verify_gemini_oauth(user_id, projects_root, keys)
    elif keys.get("gemini") or keys.get("gemini_api_key"):
        results["gemini_cli"] = verify_gemini_cli(user_id, projects_root, keys)
        
    return results
