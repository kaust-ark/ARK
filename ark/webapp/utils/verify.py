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
            "lastOnboardingVersion": version,
            "oauthAccount": {
                "accountUuid": keys.get("claude_account_uuid", ""),
                "emailAddress": keys.get("claude_email", ""),
                "organizationUuid": keys.get("claude_org_uuid", "")
            }
        }
        (verify_dir / ".claude.json").write_text(json.dumps(config))
        
        # 3. Run Claude CLI with restricted environment
        # Strip CLAUDECODE to prevent nested-session issues
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        
        # Use a model that's cheap and fast for testing (Haiku if available, else default)
        cmd = [
            "claude", "-p", "Say 'Functional' in one word",
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
        
    return results
