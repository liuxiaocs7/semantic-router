"""THROWAWAY: ablate error handling independently against unchanged HTTP/store tests.

Run: python3 run.py [variant]
Production files are restored after every run; patches and test evidence stay here.
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"]).decode().strip())
AUTH = "dashboard/backend/auth/"
BASE = "10f321b87f53ad750a4d5e9525047a017d7f8abd"
FILES = [AUTH + name for name in (
    "admin_meta_handlers.go", "admin_user_handlers.go", "invitation_handlers.go",
    "invitation_service.go", "public_handlers.go", "schema.go", "service.go",
)]
FILES += ["dashboard/backend/handlers/config_projection.go"]
BASELINE = {p: subprocess.check_output(["git", "show", BASE + ":" + p], cwd=ROOT).decode() for p in FILES}
REGRESSIONS = "AuthHandlersHide|LoginHandler.*(Errors|Messages)|AdminUser.*(NotFound|LastManager)|Invitation.*(StoreFailures|Validation|UnavailableResponse)|ConfigProjectionHandlersHideStoreErrors|ConfigDeploymentDetailNotFound"


def replace_once(files, name, before, after):
    p = AUTH + name
    assert files[p].count(before) == 1, (name, before)
    files[p] = files[p].replace(before, after)


def apply_variant(name):
    files = dict(BASELINE)
    public = "public_handlers.go"
    if name == "login-generic":
        p = AUTH + public
        start = files[p].index("\t\t\tswitch {", files[p].index("func loginHandler"))
        end = files[p].index("\n\t\t\treturn", start)
        files[p] = files[p][:start] + '\t\t\tlog.Printf("authentication failed: %v", err)\n\t\t\thttp.Error(w, "authentication failed", http.StatusUnauthorized)' + files[p][end:]
    elif name == "login-text-classification":
        p = AUTH + public
        files[p] = files[p].replace("errors.Is(err, ErrInvalidCredentials)", "err.Error() == ErrInvalidCredentials.Error()")
        files[p] = files[p].replace("errors.Is(err, ErrUserNotActive)", "err.Error() == ErrUserNotActive.Error()")
    elif name == "invitation-no-validation":
        p = AUTH + "invitation_handlers.go"
        start = files[p].index("func writeInvitationValidationError")
        files[p] = files[p][:start] + "func writeInvitationValidationError(w http.ResponseWriter, err error) bool {\n\treturn false\n}\n"
    elif name == "last-manager-generic":
        p = AUTH + "admin_user_handlers.go"
        for err, operation, action in (("validationErr", "update", "update"), ("err", "deletion", "delete")):
            before = f'''\t\tif errors.Is({err}, errLastActiveUserManager) {{
\t\t\thttp.Error(w, errLastActiveUserManager.Error(), http.StatusConflict)
\t\t}} else {{
\t\t\tlog.Printf("failed to validate user {operation}: %v", {err})
\t\t\thttp.Error(w, "failed to {action} user", http.StatusInternalServerError)
\t\t}}'''
            replace_once(files, "admin_user_handlers.go", before,
                         f'\t\tlog.Printf("failed to validate user {operation}: %v", {err})\n\t\thttp.Error(w, "failed to {action} user", http.StatusInternalServerError)')
    elif name == "invitation-collapse-lookup":
        p = AUTH + "invitation_service.go"
        files[p] = subprocess.check_output(["git", "show", "6442817ae:" + p], cwd=ROOT).decode()
    elif name == "no-diagnostic-logs":
        for p in files:
            files[p] = re.sub(r'^\s*log.Printf\([^\n]+\)\n', "\n", files[p], flags=re.M)
            if "log." not in files[p]:
                files[p] = files[p].replace('\t"log"\n', "")
    elif name in ("reuse-user-lookup", "combined"):
        replace_once(files, "admin_meta_handlers.go", '''\t\t\tif errors.Is(err, sql.ErrNoRows) {
\t\t\t\thttp.Error(w, "user not found", http.StatusNotFound)
\t\t\t} else {
\t\t\t\tlog.Printf("failed to load user for password update: %v", err)
\t\t\t\thttp.Error(w, "failed to load user", http.StatusInternalServerError)
\t\t\t}''', "\t\t\twriteUserLookupError(w, err)")
        files[AUTH + "admin_meta_handlers.go"] = files[AUTH + "admin_meta_handlers.go"].replace('\t"database/sql"\n', "")
    elif name == "shared-internal-error":
        pattern = re.compile(r'log\.Printf\("([^"\n]+): %v", (\w+)\)\n\s*http\.Error\(w, "\1", http\.StatusInternalServerError\)')
        for p in files:
            if p.startswith(AUTH):
                files[p] = pattern.sub(lambda m: f'writeInternalError(w, "{m.group(1)}", {m.group(2)})', files[p])
        files[AUTH + public] += '''
func writeInternalError(w http.ResponseWriter, message string, err error) {
\tlog.Printf("%s: %v", message, err)
\thttp.Error(w, message, http.StatusInternalServerError)
}
'''
    elif name != "unify-invitation-handler":
        raise ValueError(name)

    if name in ("unify-invitation-handler", "combined"):
        replace_once(files, "invitation_handlers.go", '''\t\t\t\tif writeAdminMutationAuthorizationError(w, err) {
\t\t\t\t\treturn
\t\t\t\t}
\t\t\t\tif writeInvitationValidationError(w, err) {
\t\t\t\t\treturn
\t\t\t\t}
\t\t\t\tlog.Printf("failed to create invitation: %v", err)
\t\t\t\thttp.Error(w, "failed to create invitation", http.StatusInternalServerError)''',
                     '\t\t\t\twriteInvitationError(w, err, "failed to create invitation")')
        p = AUTH + "invitation_handlers.go"
        files[p] = files[p].replace("writeInvitationError(w, err)", 'writeInvitationError(w, err, "failed to process invitation")')
        start = files[p].index("func writeInvitationError")
        files[p] = files[p][:start] + '''func writeInvitationError(w http.ResponseWriter, err error, message string) {
\tif writeAdminMutationAuthorizationError(w, err) {
\t\treturn
\t}
\tif errors.Is(err, ErrInvitationUnavailable) {
\t\thttp.Error(w, "invitation is no longer available", http.StatusGone)
\t\treturn
\t}
\tfor _, validationErr := range []error{
\t\tErrInvalidInvitationEmail, ErrInvalidInvitationName, ErrInvitationUserExists,
\t\tErrInvalidInvitationKind, ErrInvalidInvitationUses, errInvalidRole,
\t} {
\t\tif errors.Is(err, validationErr) {
\t\t\thttp.Error(w, validationErr.Error(), http.StatusBadRequest)
\t\t\treturn
\t\t}
\t}
\tlog.Printf("%s: %v", message, err)
\thttp.Error(w, message, http.StatusInternalServerError)
}
'''
    return files


def run(name):
    files = apply_variant(name)
    test_path = ROOT / AUTH / "invitation_store_error_test.go"
    saved_test = test_path.read_text()
    saved_production = {p: (ROOT / p).read_text() for p in FILES}
    try:
        for p, content in files.items():
            (ROOT / p).write_text(content)
        if name in ("unify-invitation-handler", "combined"):
            test_path.write_text(saved_test.replace('writeInvitationError(response, fmt.Errorf("internal details: %w", validationErr))', 'writeInvitationError(response, fmt.Errorf("internal details: %w", validationErr), "failed to process invitation")'))
        subprocess.run(["gofmt", "-w", *FILES, str(test_path)], cwd=ROOT, check=True)
        patch = subprocess.check_output(["git", "diff", "--", *FILES, str(test_path)], cwd=ROOT)
        (HERE / (name + ".patch")).write_bytes(patch)
        changed_lines = sum(len((ROOT / p).read_text().splitlines()) - len(BASELINE[p].splitlines()) for p in FILES)
        log = HERE / (name + ".jsonl")
        with log.open("w") as output:
            result = subprocess.run(["go", "test", "./auth", "./handlers", "-count=1", "-json", "-run", REGRESSIONS],
                                    cwd=ROOT / "dashboard/backend", env={**os.environ, "CGO_ENABLED": "1"}, stdout=output, stderr=subprocess.STDOUT)
        events = []
        for line in log.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        failures = [e["Test"] for e in events if e["Action"] == "fail" and e.get("Test")]
        leaf_failures = [f for f in failures if not any(other.startswith(f + "/") for other in failures)]
        report = {"variant": name, "exit_code": result.returncode, "production_line_delta": changed_lines,
                  "failed_cases": leaf_failures, "passed_cases": sum(e["Action"] == "pass" and bool(e.get("Test")) for e in events)}
        (HERE / (name + ".result.json")).write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report), flush=True)
    finally:
        for p, content in saved_production.items():
            (ROOT / p).write_text(content)
        test_path.write_text(saved_test)


if __name__ == "__main__":
    variants = sys.argv[1:] or ["login-generic", "login-text-classification", "invitation-no-validation", "last-manager-generic",
                              "invitation-collapse-lookup", "no-diagnostic-logs", "reuse-user-lookup", "unify-invitation-handler",
                              "shared-internal-error", "combined"]
    for variant in variants:
        run(variant)
