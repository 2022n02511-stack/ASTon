import json
import subprocess
from pathlib import Path

def validate_rules_against_target(
    rules: list[dict],
    pkg_name: str,
    target_version: str,
    req_file: str,
    current_version: str = None
) -> list[dict]:
    """
    Stage 2.5: Target API Environment Validation
    
    Inspected against the pinned target SDK version in Docker/sandbox.
    Degrees of failure handled:
      1. 'exists': API verified successfully in target SDK.
      2. 'missing': API symbol does not exist in target SDK version.
      3. 'unverifiable': Could not safely evaluate/traverse the API path.
    
    Mutates invalid rules to 'not_validated' and marks new_api as unresolved 
    while preserving historical records.
    """
    if not rules:
        return []

    print(f"\n==================================================")
    print(f"--- [Stage 2.5] Target API Environment Validation ---")
    print(f"==================================================")
    print(f"Target SDK Scope: {pkg_name} ({current_version or 'any'} -> {target_version})")

    probe_payload = []
    for idx, rule in enumerate(rules):
        # Ensure version bounds are stamped on the rule if missing
        if current_version and "from_version" not in rule:
            rule["from_version"] = current_version
        if target_version and "target_version" not in rule:
            rule["target_version"] = target_version

        new_api = rule.get("new_api")
        if not new_api or "UNRESOLVED" in new_api:
            rule["validation_status"] = "not_validated"
            rule["target_validation_status"] = "missing"
            continue

        probe_payload.append({
            "id": idx,
            "old_api": rule.get("old_api"),
            "new_api": new_api,
            "requires_instantiation": rule.get("requires_instantiation")
        })

    if not probe_payload:
        return rules

    # Generate the inspection probe script
    probe_code = f"""
import json
import sys

pkg_name = {pkg_name!r}
rules_to_check = {json.dumps(probe_payload)}
results = {{}}

try:
    module = __import__(pkg_name)
except Exception as e:
    print(json.dumps({{"error": f"Failed to import {{pkg_name}}: {{e}}"}}))
    sys.exit(0)

for item in rules_to_check:
    rid = item["id"]
    new_api = item["new_api"]
    requires_init = item.get("requires_instantiation")
    
    eval_target = new_api
    if eval_target.startswith("client."):
        eval_target = eval_target.replace("client.", "client_instance.", 1)

    try:
        namespace = {{pkg_name: module}}
        
        if requires_init or "client" in new_api:
            client_class = None
            for attr_name in dir(module):
                cls_candidate = getattr(module, attr_name)
                if isinstance(cls_candidate, type) and ("client" in attr_name.lower() or attr_name == "OpenAI"):
                    client_class = cls_candidate
                    break
            
            if client_class:
                try:
                    namespace["client_instance"] = client_class(api_key="test-dummy-key")
                except Exception:
                    try:
                        namespace["client_instance"] = client_class()
                    except Exception:
                        pass

        parts = eval_target.split(".")
        current = namespace
        exists = True
        for part in parts:
            if isinstance(current, dict):
                if part in current:
                    current = current[part]
                else:
                    exists = False
                    break
            else:
                if hasattr(current, part):
                    current = getattr(current, part)
                else:
                    exists = False
                    break

        results[str(rid)] = "exists" if exists else "missing"
    except Exception:
        results[str(rid)] = "unverifiable"

print(json.dumps(results))
"""

    probe_file = Path("target_probe.py")
    probe_file.write_text(probe_code)

    validation_results = {}
    try:
        # Execute probe inside Docker using the target requirements file
        output = subprocess.check_output(
            [
                "docker", "run", "--rm",
                "-v", f"{Path.cwd().resolve()}:/app",
                "-w", "/app",
                "python:3.11-slim",
                "bash", "-c", f"pip install -q -r {req_file} && python target_probe.py"
            ],
            text=True,
            timeout=60
        )
        for line in reversed(output.strip().splitlines()):
            if line.startswith("{") and line.endswith("}"):
                validation_results = json.loads(line)
                break
    except Exception as e:
        print(f"⚠️ Docker probe failed ({e}); falling back to local Python environment.")
        try:
            local_out = subprocess.check_output(["python", "target_probe.py"], text=True, timeout=15)
            for line in reversed(local_out.strip().splitlines()):
                if line.startswith("{") and line.endswith("}"):
                    validation_results = json.loads(line)
                    break
        except Exception:
            pass

    if probe_file.exists():
        probe_file.unlink()

    # Process and mutate rules based on verification status and degrees of failure
    validated_rules = []
    for idx, rule in enumerate(rules):
        rule_copy = dict(rule)
        
        # Skip if already marked missing above
        if "target_validation_status" in rule_copy and rule_copy["target_validation_status"] == "missing":
            validated_rules.append(rule_copy)
            continue

        status = validation_results.get(str(idx), "unverifiable")
        rule_copy["target_validation_status"] = status

        if status == "exists":
            rule_copy["validation_status"] = "validated"
            print(f"  ✓ Validated: {rule_copy.get('old_api')} -> {rule_copy.get('new_api')} [EXISTS]")
            
        elif status == "missing":
            rule_copy["validation_status"] = "not_validated"
            rule_copy["new_api"] = "UNRESOLVED: target API missing"
            rule_copy["description"] = (rule_copy.get("description") or "") + " [Target Verification Failed: API missing in target SDK version]"
            print(f"  ❌ Rejected (Missing): {rule_copy.get('old_api')} -> Marked as UNRESOLVED")
            
        else: # unverifiable
            rule_copy["validation_status"] = "not_validated"
            rule_copy["new_api"] = "UNRESOLVED: target api could not be verified"
            rule_copy["description"] = (rule_copy.get("description") or "") + " [Target Verification Failed: target api could not be verified]"
            print(f"  ⚠️ Unverifiable: {rule_copy.get('old_api')} -> Marked as UNRESOLVED")

        validated_rules.append(rule_copy)

    return validated_rules