#!/usr/bin/env python3
"""
Mozart Unified Dashboard
========================
A single web UI consolidating all Mozart/TestRail tools:
  1. Milestone Audit Tool - Compare two TestRail milestones for deviations
  2. Mozart Failure Analyzer - Upload ZIP and analyze test failures
  3. TestRail Mapping Tool - Copy passed results from one run to another
  4. Mozart Skip Tool - Mark passed test cases as SKIPPED in Mozart suites
  5. Auto-Rebase Tool - Rebase all mozart-workspace projects onto mainline

Usage:
    python3 app.py
    Open http://localhost:5050
"""

import os
import sys
import json
import csv
import io
import uuid
import zipfile
import tempfile
import shutil
import subprocess
import re
from pathlib import Path
from datetime import datetime

from flask import (
    Flask, render_template, request, jsonify, send_file, Response
)

# ---------------------------------------------------------------------------
# App Setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB max
app.secret_key = os.urandom(24)

UPLOAD_DIR = Path(tempfile.gettempdir()) / "mozart_dashboard_uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Configuration
#
# All environment-specific values are read from environment variables so no
# credentials or internal endpoints are hardcoded. See .env.example.
# ---------------------------------------------------------------------------
TESTRAIL_URL = os.environ.get("TESTRAIL_URL", "").rstrip("/")
WORKSPACE_PATH = os.environ.get("WORKSPACE_PATH", str(Path.home() / "mozart-workspace"))
TESTRAIL_USERNAME = os.environ.get("TESTRAIL_USERNAME", "")
TESTRAIL_API_KEY = os.environ.get("TESTRAIL_API_KEY", "")
# Host for the code review tool, e.g. "https://code.example.com" (no default)
CODE_REVIEW_HOST = os.environ.get("CODE_REVIEW_HOST", "").rstrip("/")
# Default git remote used by the Auto-Rebase tool
DEFAULT_GIT_REMOTE = os.environ.get("DEFAULT_GIT_REMOTE", "origin")
REBASE_PROJECTS = [
    "cdk", "dhal", "iotssh",
    "mozart/MozartDaemon", "mozart/MozartTests", "mozart/MozartV3"
]

STATUS_MAP = {
    1: "Passed", 2: "Blocked", 3: "Untested", 4: "Retest",
    5: "Failed", 6: "Skipped", 7: "Performance", 8: "Queried",
    9: "Parked", 10: "Running", 11: "Caution", 12: "Not applicable"
}


# ===========================================================================
# ROUTES - Pages
# ===========================================================================

@app.route("/")
def dashboard():
    """Main dashboard page."""
    return render_template("dashboard.html")


@app.route("/milestone-audit")
def milestone_audit_page():
    """Milestone Audit Tool page."""
    return render_template("milestone_audit.html")


@app.route("/failure-analyzer")
def failure_analyzer_page():
    """Mozart Failure Analyzer page."""
    return render_template("failure_analyzer.html")


@app.route("/testrail-mapping")
def testrail_mapping_page():
    """TestRail Mapping Tool page."""
    return render_template("testrail_mapping.html")


@app.route("/skip-tool")
def skip_tool_page():
    """Mozart Skip Tool page."""
    return render_template("skip_tool.html")


@app.route("/auto-rebase")
def auto_rebase_page():
    """Auto-Rebase Tool page."""
    return render_template(
        "auto_rebase.html",
        workspace_path=WORKSPACE_PATH,
        git_remote=DEFAULT_GIT_REMOTE,
    )


# ===========================================================================
# API ROUTES - Milestone Audit
# ===========================================================================

@app.route("/api/milestone-audit/run", methods=["POST"])
def api_milestone_audit_run():
    """
    Compare two milestones in TestRail.
    Expects JSON: {project_id, milestone_id_1, milestone_id_2, username, api_key}
    """
    try:
        import requests as req

        data = request.json
        project_id = data.get("project_id")
        milestone_1 = data.get("milestone_id_1")
        milestone_2 = data.get("milestone_id_2")
        username = TESTRAIL_USERNAME
        api_key = TESTRAIL_API_KEY

        if not all([project_id, milestone_1, milestone_2]):
            return jsonify({"error": "Project ID and both Milestone IDs are required"}), 400

        # Create TestRail session
        session = req.Session()
        session.auth = (username, api_key)
        session.headers.update({"Content-Type": "application/json"})
        base_api = f"{TESTRAIL_URL}/index.php?/api/v2"

        def api_get(endpoint, params=None):
            all_results = []
            offset = 0
            limit = 250
            while True:
                p = params.copy() if params else {}
                p["limit"] = limit
                p["offset"] = offset
                resp = session.get(f"{base_api}/{endpoint}", params=p)
                if resp.status_code != 200:
                    raise Exception(f"API error ({resp.status_code}): {resp.text[:200]}")
                d = resp.json()
                if isinstance(d, list):
                    all_results.extend(d)
                    break
                elif isinstance(d, dict):
                    for key in ["runs", "plans", "tests", "results", "cases"]:
                        if key in d:
                            all_results.extend(d[key])
                            break
                    else:
                        return d
                    if d.get("size", 0) < limit:
                        break
                    offset += limit
            return all_results

        def get_runs_for_milestone(mid):
            runs = api_get(f"get_runs/{project_id}", {"milestone_id": mid})
            plans = api_get(f"get_plans/{project_id}", {"milestone_id": mid})
            for plan_summary in plans:
                plan_detail = api_get(f"get_plan/{plan_summary['id']}")
                if isinstance(plan_detail, dict):
                    for entry in plan_detail.get("entries", []):
                        for run in entry.get("runs", []):
                            runs.append(run)
            return runs

        def collect_cases(mid):
            runs = get_runs_for_milestone(mid)
            cases = {}
            for run in runs:
                tests = api_get(f"get_tests/{run['id']}")
                for t in tests:
                    cid = t.get("case_id")
                    if cid and cid not in cases:
                        cases[cid] = {
                            "case_id": cid,
                            "title": t.get("title", ""),
                            "status_id": t.get("status_id", 3),
                            "status": STATUS_MAP.get(t.get("status_id", 3), "Unknown"),
                            "run_name": run.get("name", ""),
                            "run_id": run.get("id", "")
                        }
            return cases

        # Fetch both milestones
        m1_info = api_get(f"get_milestone/{milestone_1}")
        m2_info = api_get(f"get_milestone/{milestone_2}")
        m1_name = m1_info.get("name", f"Milestone {milestone_1}") if isinstance(m1_info, dict) else f"Milestone {milestone_1}"
        m2_name = m2_info.get("name", f"Milestone {milestone_2}") if isinstance(m2_info, dict) else f"Milestone {milestone_2}"

        cases_1 = collect_cases(milestone_1)
        cases_2 = collect_cases(milestone_2)

        # Find common cases with deviations
        common_ids = set(cases_1.keys()) & set(cases_2.keys())
        deviations = []
        for cid in sorted(common_ids):
            c1 = cases_1[cid]
            c2 = cases_2[cid]
            s1_pass = c1["status_id"] == 1
            s2_pass = c2["status_id"] == 1
            s1_fail = c1["status_id"] == 5
            s2_fail = c2["status_id"] == 5
            if (s1_pass and s2_fail) or (s1_fail and s2_pass):
                deviations.append({
                    "case_id": cid,
                    "title": c1["title"],
                    "m1_status": c1["status"],
                    "m1_run": c1["run_name"],
                    "m2_status": c2["status"],
                    "m2_run": c2["run_name"],
                })

        return jsonify({
            "success": True,
            "m1_name": m1_name,
            "m2_name": m2_name,
            "total_m1": len(cases_1),
            "total_m2": len(cases_2),
            "common": len(common_ids),
            "deviations": deviations,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===========================================================================
# API ROUTES - Failure Analyzer
# ===========================================================================

@app.route("/api/failure-analyzer/upload", methods=["POST"])
def api_failure_analyzer_upload():
    """
    Upload a Mozart results ZIP for analysis.
    Delegates to the existing failure_analyzer module.
    """
    try:
        # Add the failure analyzer path (configurable via env var)
        fa_path = os.environ.get(
            "FAILURE_ANALYZER_PATH",
            str(Path.home() / "mozart-failure-analyzer"),
        )
        if fa_path not in sys.path:
            sys.path.insert(0, fa_path)

        from log_parser import MozartLogParser
        from failure_analyzer import FailureAnalyzer

        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        file = request.files["file"]
        if not file.filename.endswith(".zip"):
            return jsonify({"error": "Please upload a ZIP file"}), 400

        # Save and extract
        job_id = str(uuid.uuid4())[:8]
        job_dir = UPLOAD_DIR / job_id
        job_dir.mkdir(exist_ok=True)

        zip_path = job_dir / file.filename
        file.save(str(zip_path))

        extract_dir = job_dir / "extracted"
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            zf.extractall(str(extract_dir))

        # Parse and analyze
        parser = MozartLogParser()
        run_data = parser.parse_directory(str(extract_dir))

        analyzer = FailureAnalyzer()
        results = analyzer.analyze_failures(run_data)

        # Build summary
        total_tests = len(run_data.get("test_cases", []))
        failed_tests = len([tc for tc in run_data.get("test_cases", []) if tc.get("status") == "FAILED"])
        passed_tests = len([tc for tc in run_data.get("test_cases", []) if tc.get("status") == "PASSED"])

        return jsonify({
            "success": True,
            "job_id": job_id,
            "summary": {
                "total": total_tests,
                "passed": passed_tests,
                "failed": failed_tests,
            },
            "results": results[:50],  # Limit response size
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===========================================================================
# API ROUTES - TestRail Mapping
# ===========================================================================

@app.route("/api/testrail-mapping/copy", methods=["POST"])
def api_testrail_mapping_copy():
    """
    Copy passed results from one TestRail run to another.
    Expects JSON: {parent_run_id, child_run_id}
    Uses pre-configured TestRail credentials.
    """
    try:
        import requests as req

        data = request.json
        parent_run_id = data.get("parent_run_id")
        child_run_id = data.get("child_run_id")
        username = TESTRAIL_USERNAME
        api_key = TESTRAIL_API_KEY

        if not all([parent_run_id, child_run_id]):
            return jsonify({"error": "Parent Run ID and Child Run ID are required"}), 400

        session = req.Session()
        session.auth = (username, api_key)
        session.headers.update({"Content-Type": "application/json"})
        base_api = f"{TESTRAIL_URL}/index.php?/api/v2"

        # Fetch tests from parent run
        resp = session.get(f"{base_api}/get_tests/{parent_run_id}")
        if resp.status_code != 200:
            return jsonify({"error": f"Failed to fetch parent run: {resp.status_code}"}), 400

        resp_data = resp.json()
        tests_list = resp_data.get("tests", []) if isinstance(resp_data, dict) else resp_data

        # Collect passed results
        # NOTE: get_tests only returns each test's current status_id, NOT the
        # "version" field. The version is recorded on the *result* that the
        # automation run posted. So for every passed test we read that test's
        # results from the parent run, pick the version off its passing
        # result, and copy that exact version into the child run.
        passed_results = []
        versions_mapped = 0
        for test in tests_list:
            if test.get("status_id") != 1:
                continue

            case_id = test.get("case_id")
            result_entry = {
                "case_id": case_id,
                "status_id": 1,
                "comment": f"Auto-copied from run {parent_run_id}",
            }

            # Pull the version off the automation run's passing result.
            version = None
            try:
                res_resp = session.get(
                    f"{base_api}/get_results_for_case/{parent_run_id}/{case_id}"
                )
                if res_resp.status_code == 200:
                    res_data = res_resp.json()
                    results = (
                        res_data.get("results", [])
                        if isinstance(res_data, dict)
                        else res_data
                    )
                    # Prefer the version from a passing result; fall back to the
                    # most recent result that actually has a version set.
                    for r in results:
                        if r.get("status_id") == 1 and r.get("version"):
                            version = r.get("version")
                            break
                    if not version:
                        for r in results:
                            if r.get("version"):
                                version = r.get("version")
                                break
            except Exception:
                version = None

            if version:
                result_entry["version"] = version
                versions_mapped += 1

            passed_results.append(result_entry)

        if not passed_results:
            return jsonify({"success": True, "copied": 0, "message": "No passed results to copy"})

        # Copy to child run
        payload = {"results": passed_results}
        resp = session.post(f"{base_api}/add_results_for_cases/{child_run_id}", json=payload)

        if resp.status_code == 200:
            return jsonify({
                "success": True,
                "copied": len(passed_results),
                "versions_mapped": versions_mapped,
                "total_in_parent": len(tests_list),
                "message": f"Successfully copied {len(passed_results)} passed results "
                           f"({versions_mapped} with version) to run {child_run_id}"
            })
        else:
            return jsonify({"error": f"Failed to update child run: {resp.status_code} - {resp.text[:200]}"}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===========================================================================
# API ROUTES - Mozart Skip Tool
# ===========================================================================

# Add mozart-skip-tool-web to path for imports (configurable via env var)
_skip_tool_path = os.environ.get(
    "SKIP_TOOL_PATH", str(Path.home() / "mozart-skip-tool-web")
)
if _skip_tool_path not in sys.path:
    sys.path.insert(0, _skip_tool_path)

# Server-side session storage for skip tool
_skip_storage = {}


def _skip_store(key, value):
    _skip_storage[key] = value


def _skip_retrieve(key):
    return _skip_storage.get(key)


@app.route("/api/skip-tool/testrail-fetch", methods=["POST"])
def api_skip_tool_testrail_fetch():
    """Fetch test results from a TestRail run."""
    try:
        import requests as req

        data = request.json
        run_id = str(data.get("run_id", "")).strip()
        if not run_id:
            return jsonify({"error": "Run ID is required"}), 400

        # TestRail credentials (same as testrail_mapping)
        username = TESTRAIL_USERNAME
        api_key = TESTRAIL_API_KEY
        base_api = f"{TESTRAIL_URL}/index.php?/api/v2"

        session = req.Session()
        session.auth = (username, api_key)
        session.headers.update({"Content-Type": "application/json"})

        # Fetch all tests from the run
        all_tests = []
        offset = 0
        limit = 250
        while True:
            resp = session.get(f"{base_api}/get_tests/{run_id}&limit={limit}&offset={offset}")
            if resp.status_code != 200:
                return jsonify({"error": f"TestRail API error [{resp.status_code}]: {resp.text[:200]}"}), 400
            response_data = resp.json()
            if isinstance(response_data, list):
                all_tests.extend(response_data)
                break
            elif "tests" in response_data:
                tests = response_data["tests"]
                all_tests.extend(tests)
                if len(tests) < limit:
                    break
                offset += limit
            else:
                break

        # Convert to our format
        status_map = {1: "passed", 2: "blocked", 3: "untested", 4: "retest", 5: "failed", 6: "skipped"}
        passed_cases = []
        summary = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}

        testcases = []
        for test in all_tests:
            status_id = test.get("status_id", 3)
            status = status_map.get(status_id, "untested")
            summary["total"] += 1
            tc_entry = {
                "result": status,
                "testCaseName": test.get("title", ""),
                "testCaseUUID": str(test.get("case_id", "")),
                "testClassName": test.get("title", "").split(".")[0] if "." in test.get("title", "") else "",
            }
            testcases.append(tc_entry)

            if status == "passed":
                summary["passed"] += 1
                passed_cases.append({
                    "testCaseUUID": tc_entry["testCaseUUID"],
                    "testCaseName": tc_entry["testCaseName"],
                    "testClassName": tc_entry["testClassName"],
                })
            elif status == "failed":
                summary["failed"] += 1
            elif status == "skipped":
                summary["skipped"] += 1

        # Store for preview
        results_data = {
            "project_name": f"TestRail Run #{run_id}",
            "job_uuid": run_id,
            "summary": summary,
            "passed_cases": passed_cases,
            "failed_cases": [],
            "skipped_cases": [],
            "all_cases": testcases,
        }
        _skip_store("results_data", results_data)
        _skip_store("source", "testrail")

        return jsonify({
            "success": True,
            "project_name": results_data["project_name"],
            "job_uuid": run_id,
            "summary": summary,
            "passed_cases": passed_cases[:50],  # Limit response
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/skip-tool/preview", methods=["POST"])
def api_skip_tool_preview():
    """
    Preview which cases will be skipped.
    Accepts: multipart file upload OR JSON with suite_name + source=testrail
    """
    try:
        from app.core import parse_results_file, match_to_suite
        from app.mozart_api import MozartAPIClient, MozartAPIError
        from config import Config

        suite_name = None
        results_data = None

        if request.content_type and "multipart" in request.content_type:
            # File upload source
            if "file" not in request.files:
                return jsonify({"error": "No file uploaded"}), 400
            file = request.files["file"]
            suite_name = request.form.get("suite_name", "").strip()
            content = file.read().decode("utf-8")
            raw_data = json.loads(content)
            results_data = parse_results_file(raw_data)
            _skip_store("results_data", results_data)
            _skip_store("source", "scheduler")
        else:
            # JSON source (testrail)
            data = request.json
            suite_name = data.get("suite_name", "").strip()
            results_data = _skip_retrieve("results_data")

        if not suite_name:
            return jsonify({"error": "Suite name is required"}), 400
        if not results_data:
            return jsonify({"error": "No results data. Please upload a file or fetch from TestRail first."}), 400

        # Create Mozart API client
        config = {k: getattr(Config, k) for k in dir(Config) if not k.startswith('_')}
        client = MozartAPIClient(config)

        # Lookup suite
        try:
            suite_data = client.get_suite_by_name(suite_name)
        except MozartAPIError as e:
            if e.status_code == 404:
                return jsonify({"error": f"Suite '{suite_name}' not found in Mozart"}), 404
            raise

        # Match results to suite
        preview_result = match_to_suite(results_data, suite_data)

        # Store for apply
        _skip_store("suite_data", suite_data)
        _skip_store("preview_result", preview_result)

        return jsonify({
            "success": True,
            **preview_result,
        })

    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON file"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/skip-tool/apply", methods=["POST"])
def api_skip_tool_apply():
    """Apply skip changes to the suite."""
    try:
        from app.core import build_update_payload
        from app.mozart_api import MozartAPIClient, MozartAPIError
        from config import Config

        suite_data = _skip_retrieve("suite_data")
        preview_result = _skip_retrieve("preview_result")

        if not suite_data or not preview_result:
            return jsonify({"error": "No preview data. Please run preview first."}), 400

        if not preview_result.get("newly_skipped"):
            return jsonify({"error": "No test cases to skip. All passed cases are already skipped."}), 400

        uuids_to_skip = [tc["uuid"] for tc in preview_result["newly_skipped"]]
        testlanes_payload = build_update_payload(suite_data, uuids_to_skip)

        config = {k: getattr(Config, k) for k in dir(Config) if not k.startswith('_')}
        client = MozartAPIClient(config)
        suite_uuid = preview_result["suite_uuid"]

        client.update_suite_order(suite_uuid, testlanes_payload)

        return jsonify({
            "success": True,
            "message": f"Successfully skipped {len(uuids_to_skip)} test case(s) in suite '{preview_result['suite_name']}'",
            "skipped_count": len(uuids_to_skip),
            "suite_name": preview_result["suite_name"],
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/skip-tool/unskip-preview", methods=["POST"])
def api_skip_tool_unskip_preview():
    """Preview which cases are currently skipped in a suite."""
    try:
        from app.mozart_api import MozartAPIClient, MozartAPIError
        from config import Config

        data = request.json
        suite_name = data.get("suite_name", "").strip()
        if not suite_name:
            return jsonify({"error": "Suite name is required"}), 400

        config = {k: getattr(Config, k) for k in dir(Config) if not k.startswith('_')}
        client = MozartAPIClient(config)

        try:
            suite_data = client.get_suite_by_name(suite_name)
        except MozartAPIError as e:
            if e.status_code == 404:
                return jsonify({"error": f"Suite '{suite_name}' not found in Mozart"}), 404
            raise

        # Find all currently skipped cases
        skipped_cases = []
        for lane in suite_data.get("testlanes", []):
            for mapped_tc in lane.get("testcases", []):
                if mapped_tc.get("skip"):
                    tc = mapped_tc.get("testcase") or {}
                    skipped_cases.append({
                        "uuid": tc.get("uuid", ""),
                        "name": tc.get("name", "Unknown"),
                    })

        _skip_store("unskip_suite_data", suite_data)
        _skip_store("unskip_skipped_cases", skipped_cases)

        return jsonify({
            "success": True,
            "suite_name": suite_data.get("name", suite_name),
            "suite_uuid": suite_data.get("uuid", ""),
            "skipped_count": len(skipped_cases),
            "skipped_cases": skipped_cases,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/skip-tool/unskip-apply", methods=["POST"])
def api_skip_tool_unskip_apply():
    """Unskip all skipped cases in a suite."""
    try:
        from app.mozart_api import MozartAPIClient, MozartAPIError
        from config import Config

        suite_data = _skip_retrieve("unskip_suite_data")
        skipped_cases = _skip_retrieve("unskip_skipped_cases")

        if not suite_data:
            return jsonify({"error": "No suite data. Please run unskip preview first."}), 400
        if not skipped_cases:
            return jsonify({"error": "No skipped cases to unskip."}), 400

        # Build payload with ALL skip=False
        testlanes_payload = []
        for lane in suite_data.get("testlanes", []):
            testcases = []
            for idx, mapped_tc in enumerate(lane.get("testcases", [])):
                tc = mapped_tc.get("testcase") or {}
                tc_uuid = tc.get("uuid")
                if not tc_uuid:
                    continue
                testcases.append({
                    "uuid": tc_uuid,
                    "lane_index": idx,
                    "must_pass": mapped_tc.get("must_pass") or False,
                    "skip": False,
                })
            testlanes_payload.append({
                "lane": lane.get("lane"),
                "testcases": testcases,
            })

        config = {k: getattr(Config, k) for k in dir(Config) if not k.startswith('_')}
        client = MozartAPIClient(config)
        suite_uuid = suite_data.get("uuid")

        client.update_suite_order(suite_uuid, testlanes_payload)

        return jsonify({
            "success": True,
            "message": f"Successfully unskipped {len(skipped_cases)} test case(s) in suite '{suite_data.get('name')}'",
            "unskipped_count": len(skipped_cases),
            "suite_name": suite_data.get("name"),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===========================================================================
# API ROUTES - Auto Rebase
# ===========================================================================

@app.route("/api/auto-rebase/status", methods=["GET"])
def api_auto_rebase_status():
    """Check the status of all workspace projects."""
    results = []
    for project in REBASE_PROJECTS:
        project_path = os.path.join(WORKSPACE_PATH, project)
        git_dir = os.path.join(project_path, ".git")

        if not os.path.isdir(git_dir):
            results.append({"project": project, "status": "not_a_repo", "branch": "-"})
            continue

        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=project_path, capture_output=True, text=True, timeout=10
            )
            dirty = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_path, capture_output=True, text=True, timeout=10
            )
            results.append({
                "project": project,
                "status": "dirty" if dirty.stdout.strip() else "clean",
                "branch": branch.stdout.strip(),
            })
        except Exception as e:
            results.append({"project": project, "status": "error", "branch": str(e)})

    return jsonify({"success": True, "projects": results})


@app.route("/api/auto-rebase/run", methods=["POST"])
def api_auto_rebase_run():
    """Execute rebase on selected projects."""
    data = request.json or {}
    selected = data.get("projects", REBASE_PROJECTS)
    remote = data.get("remote", DEFAULT_GIT_REMOTE)
    branch = data.get("branch", "mainline")

    results = []
    for project in selected:
        project_path = os.path.join(WORKSPACE_PATH, project)
        git_dir = os.path.join(project_path, ".git")

        if not os.path.isdir(git_dir):
            results.append({"project": project, "result": "skipped", "message": "Not a git repo"})
            continue

        try:
            # Get current branch name
            current_branch_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=project_path, capture_output=True, text=True, timeout=10
            )
            current_branch = current_branch_result.stdout.strip() if current_branch_result.returncode == 0 else "unknown"

            # Check if working tree is dirty
            dirty_check = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_path, capture_output=True, text=True, timeout=10
            )
            is_dirty = bool(dirty_check.stdout.strip())

            # Stash if dirty
            stashed = False
            if is_dirty:
                stash_result = subprocess.run(
                    ["git", "stash", "push", "-m", "auto-rebase-stash"],
                    cwd=project_path, capture_output=True, text=True, timeout=30
                )
                stashed = stash_result.returncode == 0

            # Fetch
            fetch = subprocess.run(
                ["git", "fetch", remote, branch],
                cwd=project_path, capture_output=True, text=True, timeout=60
            )
            if fetch.returncode != 0:
                error_detail = fetch.stderr.strip() or fetch.stdout.strip()
                # Pop stash if we stashed
                if stashed:
                    subprocess.run(["git", "stash", "pop"], cwd=project_path, capture_output=True, timeout=10)
                results.append({
                    "project": project,
                    "result": "failed",
                    "message": f"Fetch failed on branch '{current_branch}': {error_detail}"
                })
                continue

            # Rebase
            rebase = subprocess.run(
                ["git", "rebase", f"{remote}/{branch}"],
                cwd=project_path, capture_output=True, text=True, timeout=120
            )
            if rebase.returncode != 0:
                # Capture the conflict details before aborting
                error_detail = rebase.stderr.strip() or rebase.stdout.strip()

                # Get list of conflicting files
                conflict_files = subprocess.run(
                    ["git", "diff", "--name-only", "--diff-filter=U"],
                    cwd=project_path, capture_output=True, text=True, timeout=10
                )
                conflicting = conflict_files.stdout.strip()

                subprocess.run(["git", "rebase", "--abort"], cwd=project_path, capture_output=True, timeout=10)

                # Pop stash if we stashed
                if stashed:
                    subprocess.run(["git", "stash", "pop"], cwd=project_path, capture_output=True, timeout=10)

                # Build detailed message
                msg = f"Rebase conflicts on branch '{current_branch}' — aborted."
                if conflicting:
                    conflict_list = conflicting.split('\n')[:5]  # Show up to 5 files
                    msg += f" Conflicting files: {', '.join(conflict_list)}"
                    if len(conflicting.split('\n')) > 5:
                        msg += f" (+{len(conflicting.split(chr(10))) - 5} more)"
                elif error_detail:
                    # Truncate long error messages
                    msg += f" {error_detail[:200]}"

                results.append({"project": project, "result": "failed", "message": msg})
            else:
                # Pop stash if we stashed
                stash_msg = ""
                if stashed:
                    pop_result = subprocess.run(
                        ["git", "stash", "pop"],
                        cwd=project_path, capture_output=True, text=True, timeout=10
                    )
                    if pop_result.returncode != 0:
                        stash_msg = " (⚠️ stash pop had conflicts — check manually)"

                results.append({
                    "project": project,
                    "result": "success",
                    "message": f"Rebased '{current_branch}' onto {remote}/{branch}{stash_msg}"
                })

        except subprocess.TimeoutExpired:
            results.append({"project": project, "result": "failed", "message": "Operation timed out (>60s)"})
        except Exception as e:
            results.append({"project": project, "result": "failed", "message": str(e)})

    return jsonify({"success": True, "results": results})


# ===========================================================================
# API ROUTES - CR Comment Analyzer
# ===========================================================================

@app.route("/cr-analyzer")
def cr_analyzer_page():
    """CR Comment Analyzer page."""
    return render_template("cr_analyzer.html")


@app.route("/branch-manager")
def branch_manager_page():
    """Branch Manager page."""
    return render_template("branch_manager.html")


def classify_cr_comment(content, importance=0, author_type=""):
    """
    cr-review severity model -> dashboard category.

    Replaces the previous ad-hoc keyword checks with the same triage logic used by the
    standalone `cr-review` tool. Priority order, highest first:
      must-fix    : human "Must Fix", AutoSDE "Priority: High", security/critical
      should-fix  : human "Should Fix", AutoSDE "Blocking" rules
      suggestion  : AutoSDE "Priority: Medium" / "NonBlocking", generic suggestions
      nit         : AutoSDE "Priority: Low", human "Nit"
      info        : anything else
    """
    c = content or ""
    low = c.lower()

    # Explicit human severities take precedence.
    if "must fix" in low:
        return "must-fix"
    if "should fix" in low:
        return "should-fix"

    # AutoSDE priority marker, e.g. "* **Priority**: 🚨 High"
    pm = re.search(r"priority\**\s*:\s*([^\n]*)", low)
    if pm:
        pr = pm.group(1)
        if "high" in pr:
            return "must-fix"
        if "medium" in pr:
            return "suggestion"
        if "low" in pr:
            return "nit"

    # Security / critical signals.
    if "SECURITY" in c.upper() or "🛑" in c or "🚨" in c or "critical" in low:
        return "must-fix"

    # Explicit nit.
    if "**nit" in low or "🎨" in c:
        return "nit"

    # CR metadata importance flag (blocking).
    if importance and importance >= 1:
        return "must-fix"

    # AutoSDE rule-id blocking / non-blocking hints.
    if author_type == "AAA":
        if "nonblocking" in low:
            return "suggestion"
        if "blocking" in low:
            return "should-fix"

    if "💡" in c or "suggestion" in low:
        return "suggestion"

    return "info"


@app.route("/api/cr-analyzer/fetch", methods=["POST"])
def api_cr_analyzer_fetch():
    """
    Fetch and analyze comments from a Code Review.
    Expects JSON: {cr_id} or {cr_url}
    Uses the code review auth cookie for authentication.
    """
    try:
        import requests as req

        data = request.json
        cr_input = data.get("cr_id", "").strip()

        if not cr_input:
            return jsonify({"error": "CR ID or URL is required"}), 400

        # Extract CR ID from URL or raw input
        cr_id = cr_input
        revision = "1"
        if cr_input.startswith("http://") or cr_input.startswith("https://"):
            # Extract CR-XXXXXXX from URL
            import re
            match = re.search(r'(CR-\d+)', cr_input)
            if match:
                cr_id = match.group(1)
            # Extract revision if present
            rev_match = re.search(r'/revisions/(\d+)', cr_input)
            if rev_match:
                revision = rev_match.group(1)
        elif not cr_id.startswith("CR-"):
            cr_id = f"CR-{cr_id}"

        if not CODE_REVIEW_HOST:
            return jsonify({"error": "CODE_REVIEW_HOST is not configured. Set it in your environment to use the CR Analyzer."}), 400

        # Read auth cookie for the code review host
        cookie_path = os.path.expanduser(os.environ.get("CODE_REVIEW_COOKIE", "~/.review/cookie"))
        if not os.path.exists(cookie_path):
            return jsonify({"error": "Code review auth cookie not found. Please authenticate to your code review host."}), 401

        # Use curl with the auth cookie (handles netscape cookie format properly)
        import subprocess
        result = subprocess.run(
            ["curl", "-s", "-L", "-b", cookie_path,
             "-H", "Accept: application/json",
             "-H", "Content-Type: application/json",
             f"{CODE_REVIEW_HOST}/reviews/{cr_id}/revisions/{revision}"],
            capture_output=True, text=True, timeout=30
        )

        if result.returncode != 0:
            return jsonify({"error": f"Failed to fetch CR: {result.stderr}"}), 500

        try:
            cr_data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return jsonify({"error": "Failed to parse CR response. Your auth cookie may be expired — please re-authenticate."}), 401

        # Extract revision data
        rev_data = cr_data.get("revision", {}).get("cr_revision", {})
        comments = rev_data.get("comments", [])
        summary = rev_data.get("summary", "")
        description = rev_data.get("description", "")
        author = rev_data.get("author", {}).get("entity_id", {}).get("id", "")
        status = rev_data.get("status", "")

        # =============================================================
        # Analyze, categorize, and MERGE overlapping comments
        # (AutoSDE + human reviewer → single consolidated action item)
        # =============================================================
        import re

        raw_comments = []
        for comment_wrapper in comments:
            c = comment_wrapper.get("cr_comment", {})
            author_info = c.get("author", {}).get("entity_id", {})
            comment_author = author_info.get("id", "")
            author_type = author_info.get("type", "")
            content = c.get("content", "")
            importance = c.get("importance", 0)
            is_fixed = c.get("fixed", False)
            location = c.get("location", {}).get("comment_location", {}).get("location", "")
            created_at = c.get("created_at", "")

            # Skip pure system/infra bots (not code-review related)
            skip_authors = {"CodeApprovers", "CoverlayWorker", "GK-CRUX-Analyzer",
                           "CR Detective", "Change Guardian", "InclusiveTechScanner",
                           "Security Code Scanner", "Software Assurance", "Region Flex CRUX Analyzer"}
            if comment_author in skip_authors:
                continue

            # Determine category via the shared cr-review severity model
            category = classify_cr_comment(content, importance, author_type)

            # Extract file path and line from location
            file_path = ""
            line_info = ""
            location_key = ""
            if location and location.startswith("v"):
                parts = location.split(":")
                if len(parts) >= 3:
                    file_path = parts[2] if parts[2] else ""
                if len(parts) >= 5:
                    line_info = f"L{parts[4]}" if parts[4] else ""
                location_key = f"{file_path}:{parts[4] if len(parts) >= 5 else ''}"
            elif location == "TOP" or "TOP" in location:
                file_path = "(Overall CR comment)"
                location_key = f"TOP_{id(c)}"

            is_human = author_type == "USER"

            # Extract structured fields from content
            issue = ""
            impact = ""
            solution = ""
            fix_code = ""
            vulnerability = ""

            # Parse AutoSDE structured format
            issue_match = re.search(r'\*\*Issue\*\*:\s*(.+?)(?:\n|$)', content)
            if issue_match:
                issue = issue_match.group(1).strip()
            impact_match = re.search(r'\*\*Impact\*\*:\s*(.+?)(?:\n|$)', content)
            if impact_match:
                impact = impact_match.group(1).strip()
            solution_match = re.search(r'\*\*Solution\*\*:\s*([\s\S]+?)(?:\n\*\*|$)', content)
            if solution_match:
                sol_text = solution_match.group(1).strip()
                # Remove code blocks from solution text
                solution = re.sub(r'```[\s\S]*?```', '', sol_text).strip()
                if len(solution) > 300:
                    solution = solution[:300] + "..."
            vuln_match = re.search(r'\*\*Vulnerability\*\*:\s*(.+?)(?:\n|$)', content)
            if vuln_match:
                vulnerability = vuln_match.group(1).strip()

            # Parse human reviewer comments
            if not issue and is_human:
                human_match = re.search(r'\*\*(?:Should Fix|Nit|Must Fix)\*\*:\s*([\s\S]+?)(?:\n\n|$)', content)
                if human_match:
                    issue = human_match.group(1).strip()
                    # Remove code blocks for summary
                    issue = re.sub(r'```[\s\S]*?```', '', issue).strip()
                    if len(issue) > 400:
                        issue = issue[:400] + "..."
                else:
                    clean = re.sub(r'\[//\]:.*?\n', '', content)
                    clean = re.sub(r'\[\[.*?\]\]', '', clean)
                    para_lines = [l.strip() for l in clean.split('\n') if l.strip() and not l.startswith('[') and not l.startswith('#')]
                    issue = ' '.join(para_lines[:3])[:300]

            # Extract code blocks from the comment (suggested fix snippets)
            code_blocks = re.findall(r'```(?:python)?\n(.*?)```', content, re.DOTALL)
            if code_blocks:
                fix_code = code_blocks[0].strip()

            # We'll determine original_code after merging, using the actual file + git diff
            original_code = ""

            raw_comments.append({
                "author": comment_author,
                "author_type": author_type,
                "is_human": is_human,
                "category": category,
                "importance": importance,
                "is_fixed": is_fixed,
                "file": file_path,
                "line": line_info,
                "location_key": location_key,
                "content": content,
                "issue": issue or vulnerability,
                "impact": impact,
                "solution": solution,
                "vulnerability": vulnerability,
                "fix_code": fix_code,
                "original_code": original_code,
                "created_at": created_at,
            })

        # =============================================================
        # MERGE: Group by file+line → one action item per issue
        # =============================================================
        priority_order = {"must-fix": 0, "should-fix": 1, "suggestion": 2, "nit": 3, "info": 4}

        location_groups = {}
        for c in raw_comments:
            key = c["location_key"] or f"_unique_{id(c)}"
            if key not in location_groups:
                location_groups[key] = []
            location_groups[key].append(c)

        merged_actions = []
        for loc_key, group in location_groups.items():
            human_comments = [c for c in group if c["is_human"]]
            bot_comments = [c for c in group if not c["is_human"]]

            # Highest severity wins
            best_category = min(group, key=lambda x: priority_order.get(x["category"], 5))["category"]
            any_fixed = any(c["is_fixed"] for c in group)

            # Merge issue description: human > bot
            merged_issue = ""
            if human_comments and human_comments[0]["issue"]:
                merged_issue = human_comments[0]["issue"]
            elif bot_comments:
                merged_issue = bot_comments[0]["issue"]

            # Merge impact: combine
            impacts = list(set(c["impact"] for c in group if c["impact"]))
            merged_impact = " ".join(impacts)

            # Merge solution: human preferred (more contextual)
            merged_solution = ""
            if human_comments:
                for hc in human_comments:
                    if hc["solution"]:
                        merged_solution = hc["solution"]
                        break
            if not merged_solution:
                for bc in bot_comments:
                    if bc["solution"]:
                        merged_solution = bc["solution"]
                        break

            # Merge fix code: human preferred
            merged_fix = ""
            if human_comments:
                for hc in human_comments:
                    if hc["fix_code"]:
                        merged_fix = hc["fix_code"]
                        break
            if not merged_fix:
                for bc in bot_comments:
                    if bc["fix_code"]:
                        merged_fix = bc["fix_code"]
                        break

            # Merge original_code: the code that needs to be found/replaced
            merged_original = ""
            for c in group:
                if c.get("original_code"):
                    merged_original = c["original_code"]
                    break

            # Sources
            sources = [{"author": c["author"], "is_human": c["is_human"], "category": c["category"]}
                       for c in group]

            merged_actions.append({
                "category": best_category,
                "file": group[0]["file"],
                "line": group[0]["line"],
                "is_fixed": any_fixed,
                "issue": merged_issue,
                "impact": merged_impact,
                "solution": merged_solution,
                "fix_code": merged_fix,
                "original_code": merged_original,
                "sources": sources,
                "num_comments": len(group),
            })

        # Sort by priority
        merged_actions.sort(key=lambda x: priority_order.get(x["category"], 5))

        # =============================================================
        # POST-PROCESS: Find the actual original code from the file
        # using git diff to identify what the CR changed
        # =============================================================
        for action in merged_actions:
            if not action["file"] or action["file"] == "(Overall CR comment)":
                continue

            try:
                # Find the file in workspace
                file_full_path = None
                for search_root in [
                    os.path.join(WORKSPACE_PATH, "dhal"),
                    os.path.join(WORKSPACE_PATH, "mozart", "MozartDaemon"),
                    os.path.join(WORKSPACE_PATH, "mozart", "MozartTests"),
                ]:
                    candidate = os.path.join(search_root, action["file"])
                    if os.path.isfile(candidate):
                        file_full_path = candidate
                        break

                if not file_full_path:
                    continue

                # Read the file
                with open(file_full_path, "r") as _f:
                    file_lines = _f.readlines()

                # Get the diff line number from the CR comment
                line_num = None
                if action["line"]:
                    try:
                        line_num = int(action["line"].replace("L", "").strip())
                    except ValueError:
                        pass

                if not line_num:
                    continue

                # Use git diff to find the added lines in this file
                # The line number from CR is the NEW file line number (after the change)
                repo_root = os.path.dirname(file_full_path)
                # Walk up to find .git
                while repo_root and not os.path.isdir(os.path.join(repo_root, ".git")):
                    parent = os.path.dirname(repo_root)
                    if parent == repo_root:
                        break
                    repo_root = parent

                # Get the diff of this file against mainline
                rel_path = os.path.relpath(file_full_path, repo_root)
                diff_result = subprocess.run(
                    ["git", "diff", "mainline", "--", rel_path],
                    cwd=repo_root, capture_output=True, text=True, timeout=10
                )

                if diff_result.returncode == 0 and diff_result.stdout:
                    # Parse diff to find the hunk containing our line
                    diff_lines = diff_result.stdout.split('\n')
                    # Find added lines around the target line number
                    # Hunks look like: @@ -old_start,old_count +new_start,new_count @@
                    current_new_line = 0
                    in_target_hunk = False
                    hunk_added_lines = []
                    hunk_start_new = 0

                    for dl in diff_lines:
                        hunk_match = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', dl)
                        if hunk_match:
                            # Save previous hunk if it was our target
                            if in_target_hunk and hunk_added_lines:
                                break

                            hunk_start_new = int(hunk_match.group(1))
                            hunk_count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
                            current_new_line = hunk_start_new
                            in_target_hunk = (hunk_start_new <= line_num <= hunk_start_new + hunk_count + 5)
                            hunk_added_lines = []
                            continue

                        if in_target_hunk:
                            if dl.startswith('+') and not dl.startswith('+++'):
                                hunk_added_lines.append(dl[1:])  # Remove the + prefix
                            elif dl.startswith('-') or dl.startswith(' '):
                                pass  # Context/removed lines

                    # The added lines from the diff ARE the original code that the reviewer
                    # is commenting on (it's code the author added that needs fixing)
                    if hunk_added_lines:
                        # Find these lines in the actual file to get the exact match with indentation
                        # Search for the first added line in the file around the target line
                        search_start = max(0, line_num - 10)
                        search_end = min(len(file_lines), line_num + len(hunk_added_lines) + 10)

                        first_added = hunk_added_lines[0].strip()
                        for i in range(search_start, search_end):
                            if first_added and first_added in file_lines[i]:
                                # Found the start — grab the block
                                block_end = min(i + len(hunk_added_lines), len(file_lines))
                                action["original_code"] = ''.join(file_lines[i:block_end]).rstrip('\n')
                                break
                else:
                    # No diff available — try to find code around the line number
                    # Read a context window around the target line
                    if line_num and line_num <= len(file_lines):
                        # Get ~5 lines around the target
                        start = max(0, line_num - 3)
                        end = min(len(file_lines), line_num + 5)
                        action["original_code"] = ''.join(file_lines[start:end]).rstrip('\n')

            except Exception:
                pass  # Best effort — don't fail the whole response

        # Count by category
        category_counts = {}
        for c in merged_actions:
            cat = c["category"]
            category_counts[cat] = category_counts.get(cat, 0) + 1

        # Group by file
        by_file = {}
        for c in merged_actions:
            f = c["file"] or "(general)"
            if f not in by_file:
                by_file[f] = []
            by_file[f].append(c)

        return jsonify({
            "success": True,
            "cr_id": cr_id,
            "revision": revision,
            "summary": summary,
            "author": author,
            "status": status,
            "total_actions": len(merged_actions),
            "total_raw_comments": len(raw_comments),
            "category_counts": category_counts,
            "actions": merged_actions,
            "by_file": by_file,
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Request timed out. Check your network connection."}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cr-analyzer/generate-fix", methods=["POST"])
def api_cr_generate_fix():
    """
    Generate the complete corrected code using AI (Kiro CLI).
    Sends original code + reviewer comments to the AI and gets back the fixed code.
    """
    try:
        import re as regex

        data = request.json
        original_code = data.get("original_code", "").strip()
        comments = data.get("comments_summary", "").strip()
        file_path = data.get("file", "")

        if not original_code:
            return jsonify({"error": "original_code is required"}), 400

        # Build the prompt for the AI
        prompt = (
            "Fix the following Python code based on the code review feedback. "
            "Output ONLY the complete fixed code block with NO explanations, NO markdown backticks, "
            "just the corrected Python code preserving the exact same indentation style.\n\n"
            f"ORIGINAL CODE:\n{original_code}\n\n"
            f"REVIEWER FEEDBACK:\n{comments}\n\n"
            f"FILE: {file_path}\n\n"
            "Output ONLY the fixed code, nothing else:"
        )

        # Call Kiro CLI (q chat) for AI generation
        result = subprocess.run(
            ["q", "chat", prompt, "--no-interactive"],
            capture_output=True, text=True, timeout=60
        )

        if result.returncode != 0:
            return jsonify({"error": f"AI generation failed: {result.stderr[:200]}"}), 500

        # Parse the output - strip ANSI codes and extract the code
        raw_output = result.stdout

        # Remove ANSI escape sequences
        ansi_escape = regex.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        clean_output = ansi_escape.sub('', raw_output)

        # Remove warning lines, prompt markers, timing info
        lines = clean_output.split('\n')
        code_lines = []
        skip_leading = True
        for line in lines:
            if 'Warning!' in line and 'Kiro' in line:
                continue
            if 'Time:' in line and 's' in line:
                continue
            if line.strip() == '>' or line.strip() == '> ':
                continue
            if skip_leading and not line.strip():
                continue
            skip_leading = False
            code_lines.append(line)

        # Clean up trailing empty lines
        while code_lines and not code_lines[-1].strip():
            code_lines.pop()

        fix_code = '\n'.join(code_lines)

        # If the AI wrapped it in ``` blocks, strip those
        if '```' in fix_code:
            fix_code = regex.sub(r'^```\w*\n?', '', fix_code)
            fix_code = regex.sub(r'\n?```\s*$', '', fix_code)

        if not fix_code.strip():
            return jsonify({"error": "AI returned empty response. Try again."}), 400

        return jsonify({
            "success": True,
            "fix_code": fix_code,
            "changed": fix_code.strip() != original_code.strip(),
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": "AI generation timed out (60s). Try with a smaller code block."}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cr-analyzer/create-branch", methods=["POST"])
def api_cr_create_branch():
    """
    Create a new branch for CR fixes in the DHAL workspace.
    Expects JSON: {cr_id, package}
    """
    try:
        data = request.json
        cr_id = data.get("cr_id", "").strip()
        package = data.get("package", "dhal").strip().lower()

        if not cr_id:
            return jsonify({"error": "CR ID is required"}), 400

        # Map package to workspace path
        package_paths = {
            "dhal": os.path.join(WORKSPACE_PATH, "dhal"),
            "mozartdaemon": os.path.join(WORKSPACE_PATH, "mozart", "MozartDaemon"),
            "mozarttests": os.path.join(WORKSPACE_PATH, "mozart", "MozartTests"),
            "mozartv3": os.path.join(WORKSPACE_PATH, "mozart", "MozartV3"),
        }

        # Default to dhal
        workspace = package_paths.get(package, package_paths["dhal"])
        if not os.path.isdir(workspace):
            return jsonify({"error": f"Workspace not found: {workspace}"}), 400

        # Create branch name from CR ID
        branch_name = f"cr-fix/{cr_id}"

        # Check current branch
        current = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=workspace, capture_output=True, text=True, timeout=10
        )
        current_branch = current.stdout.strip()

        # Check if branch already exists
        existing = subprocess.run(
            ["git", "branch", "--list", branch_name],
            cwd=workspace, capture_output=True, text=True, timeout=10
        )
        if existing.stdout.strip():
            # Branch exists, just checkout
            checkout = subprocess.run(
                ["git", "checkout", branch_name],
                cwd=workspace, capture_output=True, text=True, timeout=10
            )
            if checkout.returncode != 0:
                return jsonify({"error": f"Failed to checkout branch: {checkout.stderr}"}), 500
            return jsonify({
                "success": True,
                "branch": branch_name,
                "message": f"Switched to existing branch '{branch_name}'",
                "workspace": workspace,
            })

        # Create and checkout new branch
        create = subprocess.run(
            ["git", "checkout", "-b", branch_name],
            cwd=workspace, capture_output=True, text=True, timeout=10
        )
        if create.returncode != 0:
            return jsonify({"error": f"Failed to create branch: {create.stderr}"}), 500

        return jsonify({
            "success": True,
            "branch": branch_name,
            "previous_branch": current_branch,
            "message": f"Created and switched to branch '{branch_name}' in {workspace}",
            "workspace": workspace,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cr-analyzer/apply-fix", methods=["POST"])
def api_cr_apply_fix():
    """
    Apply a suggested code fix to the file in the workspace.
    
    Strategy:
    1. Read the target file
    2. Find the original code (search_code) in the file
    3. Replace it with fix_code
    
    Expects JSON: {file, search_code, fix_code}
    - file: relative path like src/adapt_dhal/...
    - search_code: the exact original code to find and replace
    - fix_code: the replacement code
    """
    try:
        data = request.json
        file_path = data.get("file", "").strip()
        search_code = data.get("search_code", "").strip()
        fix_code = data.get("fix_code", "").strip()

        if not file_path or not fix_code or not search_code:
            return jsonify({"error": "file, search_code, and fix_code are required"}), 400

        # Resolve the full path
        full_path = None
        search_roots = [
            os.path.join(WORKSPACE_PATH, "dhal"),
            os.path.join(WORKSPACE_PATH, "mozart", "MozartDaemon"),
            os.path.join(WORKSPACE_PATH, "mozart", "MozartTests"),
        ]

        for root in search_roots:
            candidate = os.path.join(root, file_path)
            if os.path.isfile(candidate):
                full_path = candidate
                break

        if not full_path:
            if os.path.isfile(file_path):
                full_path = file_path
            else:
                return jsonify({"error": f"File not found: {file_path}"}), 404

        # Read the file
        with open(full_path, "r") as f:
            content = f.read()

        # Try exact match first
        if search_code in content:
            new_content = content.replace(search_code, fix_code, 1)
            with open(full_path, "w") as f:
                f.write(new_content)
            return jsonify({
                "success": True,
                "file": full_path,
                "match_type": "exact",
                "message": f"Applied fix to {file_path} (exact match replaced)",
            })

        # Try matching with normalized whitespace (strip trailing spaces per line)
        def normalize(s):
            return '\n'.join(line.rstrip() for line in s.split('\n'))

        norm_content = normalize(content)
        norm_search = normalize(search_code)

        if norm_search in norm_content:
            # Find the position in normalized content and map back
            start = norm_content.index(norm_search)
            end = start + len(norm_search)
            # Replace in original content at the same char positions
            new_content = content[:start] + fix_code + content[end:]
            with open(full_path, "w") as f:
                f.write(new_content)
            return jsonify({
                "success": True,
                "file": full_path,
                "match_type": "normalized",
                "message": f"Applied fix to {file_path} (whitespace-normalized match)",
            })

        # Try line-by-line fuzzy match: find lines that contain the key parts of search_code
        search_lines = [l.strip() for l in search_code.split('\n') if l.strip()]
        if search_lines:
            content_lines = content.split('\n')
            # Find the first line of search_code in the file
            first_search_line = search_lines[0]
            match_start = None
            for i, cl in enumerate(content_lines):
                if first_search_line in cl.strip() or cl.strip() in first_search_line:
                    # Verify subsequent lines match too
                    all_match = True
                    for j, sl in enumerate(search_lines[1:], 1):
                        if i + j >= len(content_lines):
                            all_match = False
                            break
                        if sl not in content_lines[i + j].strip() and content_lines[i + j].strip() not in sl:
                            all_match = False
                            break
                    if all_match:
                        match_start = i
                        break

            if match_start is not None:
                match_end = match_start + len(search_lines)
                # Preserve the indentation of the original first line
                original_indent = content_lines[match_start][:len(content_lines[match_start]) - len(content_lines[match_start].lstrip())]
                
                # Apply fix with proper indentation
                fix_lines = fix_code.split('\n')
                indented_fix = []
                for k, fl in enumerate(fix_lines):
                    if fl.strip():
                        indented_fix.append(original_indent + fl.strip())
                    else:
                        indented_fix.append('')

                new_lines = content_lines[:match_start] + indented_fix + content_lines[match_end:]
                new_content = '\n'.join(new_lines)

                with open(full_path, "w") as f:
                    f.write(new_content)
                return jsonify({
                    "success": True,
                    "file": full_path,
                    "match_type": "fuzzy",
                    "line": match_start + 1,
                    "message": f"Applied fix to {file_path} at line {match_start + 1} (fuzzy match)",
                })

        return jsonify({
            "error": f"Could not find the original code in {file_path}. The file may have changed since the CR was created. Please apply manually.",
            "search_code_preview": search_code[:200],
        }), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===========================================================================
# API ROUTES - Branch Manager
# ===========================================================================

BRANCH_MANAGER_REPOS = {
    "dhal": os.path.join(WORKSPACE_PATH, "dhal"),
    "MozartTests": os.path.join(WORKSPACE_PATH, "mozart", "MozartTests"),
}


@app.route("/api/branches/list", methods=["POST"])
def api_branches_list():
    """List all branches for a repo."""
    try:
        data = request.json
        repo = data.get("repo", "dhal")
        repo_path = BRANCH_MANAGER_REPOS.get(repo)

        if not repo_path or not os.path.isdir(repo_path):
            return jsonify({"error": f"Repository not found: {repo}"}), 400

        # Get current branch
        current = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=10
        )
        current_branch = current.stdout.strip()

        # Get all local branches with last commit info
        result = subprocess.run(
            ["git", "branch", "--format=%(refname:short)|%(committerdate:relative)|%(subject)"],
            cwd=repo_path, capture_output=True, text=True, timeout=10
        )

        branches = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split('|', 2)
            name = parts[0].strip()
            last_commit = parts[1].strip() if len(parts) > 1 else ""
            subject = parts[2].strip() if len(parts) > 2 else ""
            branches.append({
                "name": name,
                "is_current": name == current_branch,
                "last_commit": last_commit,
                "subject": subject[:80],
            })

        # Sort: current first, then alphabetical
        branches.sort(key=lambda b: (not b["is_current"], b["name"]))

        return jsonify({
            "success": True,
            "repo": repo,
            "repo_path": repo_path,
            "current_branch": current_branch,
            "total": len(branches),
            "branches": branches,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/branches/delete", methods=["POST"])
def api_branches_delete():
    """Delete one or more branches."""
    try:
        data = request.json
        repo = data.get("repo", "dhal")
        branch_names = data.get("branches", [])

        if not branch_names:
            return jsonify({"error": "No branches specified"}), 400

        repo_path = BRANCH_MANAGER_REPOS.get(repo)
        if not repo_path:
            return jsonify({"error": f"Repository not found: {repo}"}), 400

        # Get current branch to prevent deleting it
        current = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=10
        )
        current_branch = current.stdout.strip()

        results = []
        for branch in branch_names:
            branch = branch.strip()
            if branch == current_branch:
                results.append({"branch": branch, "result": "skipped", "message": "Cannot delete current branch"})
                continue
            if branch in ("mainline", "main", "master"):
                results.append({"branch": branch, "result": "skipped", "message": "Cannot delete protected branch"})
                continue

            # Delete the branch
            delete = subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )
            if delete.returncode == 0:
                results.append({"branch": branch, "result": "deleted", "message": "Deleted successfully"})
            else:
                results.append({"branch": branch, "result": "failed", "message": delete.stderr.strip()})

        deleted_count = sum(1 for r in results if r["result"] == "deleted")
        return jsonify({
            "success": True,
            "deleted": deleted_count,
            "total": len(branch_names),
            "results": results,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/branches/checkout", methods=["POST"])
def api_branches_checkout():
    """Switch to a branch."""
    try:
        data = request.json
        repo = data.get("repo", "dhal")
        branch = data.get("branch", "").strip()

        if not branch:
            return jsonify({"error": "Branch name is required"}), 400

        repo_path = BRANCH_MANAGER_REPOS.get(repo)
        if not repo_path:
            return jsonify({"error": f"Repository not found: {repo}"}), 400

        result = subprocess.run(
            ["git", "checkout", branch],
            cwd=repo_path, capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return jsonify({"error": f"Checkout failed: {result.stderr.strip()}"}), 400

        return jsonify({
            "success": True,
            "message": f"Switched to branch '{branch}'",
            "branch": branch,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/branches/modified", methods=["POST"])
def api_branches_modified():
    """List modified/dirty files in a repo."""
    try:
        data = request.json
        repo = data.get("repo", "dhal")
        repo_path = BRANCH_MANAGER_REPOS.get(repo)

        if not repo_path:
            return jsonify({"error": f"Repository not found: {repo}"}), 400

        # Get modified files
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path, capture_output=True, text=True, timeout=10
        )

        files = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            status = line[:2].strip()
            filepath = line[3:].strip()
            status_label = {
                'M': 'Modified',
                'A': 'Added',
                'D': 'Deleted',
                '??': 'Untracked',
                'R': 'Renamed',
            }.get(status, status)
            files.append({
                "status": status,
                "status_label": status_label,
                "file": filepath,
                "can_revert": status in ('M', 'D', 'A'),
            })

        return jsonify({
            "success": True,
            "repo": repo,
            "total": len(files),
            "files": files,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/branches/revert", methods=["POST"])
def api_branches_revert():
    """Revert (discard changes) on specific files or all files."""
    try:
        data = request.json
        repo = data.get("repo", "dhal")
        files = data.get("files", [])
        revert_all = data.get("revert_all", False)

        repo_path = BRANCH_MANAGER_REPOS.get(repo)
        if not repo_path:
            return jsonify({"error": f"Repository not found: {repo}"}), 400

        results = []

        if revert_all:
            # Revert all tracked changes
            result = subprocess.run(
                ["git", "checkout", "--", "."],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                results.append({"file": "(all files)", "result": "reverted", "message": "All changes reverted"})
            else:
                results.append({"file": "(all files)", "result": "failed", "message": result.stderr.strip()})
        else:
            if not files:
                return jsonify({"error": "No files specified"}), 400

            for filepath in files:
                filepath = filepath.strip()
                # Check if it's untracked
                status_check = subprocess.run(
                    ["git", "status", "--porcelain", filepath],
                    cwd=repo_path, capture_output=True, text=True, timeout=10
                )
                status_line = status_check.stdout.strip()

                if status_line.startswith('??'):
                    # Untracked file — need to remove it
                    full_file = os.path.join(repo_path, filepath)
                    try:
                        os.remove(full_file)
                        results.append({"file": filepath, "result": "deleted", "message": "Untracked file removed"})
                    except OSError as e:
                        results.append({"file": filepath, "result": "failed", "message": str(e)})
                else:
                    # Tracked file — git checkout
                    revert = subprocess.run(
                        ["git", "checkout", "--", filepath],
                        cwd=repo_path, capture_output=True, text=True, timeout=10
                    )
                    if revert.returncode == 0:
                        results.append({"file": filepath, "result": "reverted", "message": "Changes discarded"})
                    else:
                        results.append({"file": filepath, "result": "failed", "message": revert.stderr.strip()})

        reverted_count = sum(1 for r in results if r["result"] in ("reverted", "deleted"))
        return jsonify({
            "success": True,
            "reverted": reverted_count,
            "total": len(results),
            "results": results,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===========================================================================
# Main
# ===========================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  Mozart Unified Dashboard")
    print("  http://localhost:5050")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5050, debug=True)
