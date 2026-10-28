"""Generate a labelled synthetic test set of failed deployment logs.

Each scenario has a known root cause (a knowledge base id) and usually some downstream
symptoms, so the evaluation checks that the agent finds the cause and not the loudest error.
Run:  python scripts/generate_logs.py   (writes samples/eval/*.log and samples/eval/labels.json)
"""
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "samples" / "eval"
SERVICES = ["cart-api", "auth-service", "ledger", "search-indexer", "notify-worker", "profile-api", "quote-engine"]
NAMESPACES = ["shop-staging", "payments-dev", "core-qa", "platform-staging"]


def boot(svc):
    return [("INFO", "kubelet", f"Pulling image \"registry.example.com/team/{svc}:1.{random.randint(2, 9)}.{random.randint(0, 20)}\""),
            ("INFO", "kubelet", f"Started container {svc}"),
            ("INFO", svc, f"Starting {svc}"),
            ("INFO", svc, "Loading configuration")]


def crash(svc, pod):
    return [("WARN", "kubelet", f"Back-off restarting failed container {svc} in pod {pod}"),
            ("ERROR", "kubelet", f"Pod {pod} status CrashLoopBackOff")]


def probe():
    return [("WARN", "kubelet", "Readiness probe failed: HTTP probe failed with statuscode: 503")]


def lb(svc):
    return [("ERROR", "ingress-nginx", f"502 Bad Gateway: no healthy upstream for {svc}.example.com")]


def scenarios(svc, ns, pod):
    v = random.choice
    return {
        "container_oom_killed": boot(svc) + [
            ("WARN", svc, "heap used 498MB of 512MB"),
            ("ERROR", svc, v(["FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory",
                              "java.lang.OutOfMemoryError: Java heap space"])),
            ("WARN", "kubelet", f"Container {svc} in pod {pod} was OOMKilled (exit code 137)")] + probe() + crash(svc, pod),
        "image_pull_not_found": [
            ("INFO", "deploy", f"Applying deployment \"{svc}\" in namespace {ns}"),
            ("WARN", "kubelet", f"Failed to pull image \"ghcr.io/team/{svc}:v{random.randint(1, 4)}.0.0-rc\": not found: manifest unknown"),
            ("WARN", "kubelet", "Error: ErrImagePull"), ("WARN", "kubelet", "Error: ImagePullBackOff"),
            ("ERROR", "deploy", f"deployment \"{svc}\" exceeded its progress deadline")],
        "registry_auth_failed": [
            ("INFO", "kubelet", f"Pulling image \"private.registry.io/team/{svc}:2.1.0\""),
            ("WARN", "kubelet", v([f"Failed to pull image: pull access denied for private.registry.io/team/{svc}, repository does not exist or may require 'docker login'",
                                   "Failed to pull image: unauthorized: authentication required"])),
            ("WARN", "kubelet", "Error: ImagePullBackOff")],
        "npm_dependency_conflict": [
            ("INFO", "build", "Step 4/8 : RUN npm install"),
            ("ERROR", "build", "npm ERR! code ERESOLVE"),
            ("ERROR", "build", "npm ERR! ERESOLVE unable to resolve dependency tree"),
            ("ERROR", "build", "The command '/bin/sh -c npm install' returned a non-zero code: 1")],
        "native_module_build_failed": [
            ("INFO", "build", "Step 5/9 : RUN npm ci"),
            ("WARN", "build", f"prebuild-install warn install No prebuilt binaries found (target=20.11.1 runtime=node arch=x64)"),
            ("ERROR", "build", "gyp ERR! build error"),
            ("ERROR", "build", "gyp ERR! stack Error: `make` failed with exit code: 2"),
            ("ERROR", "build", "The command '/bin/sh -c npm ci' returned a non-zero code: 1")],
        "pip_no_matching_distribution": [
            ("INFO", "build", "Step 3/7 : RUN pip install -r requirements.txt"),
            ("ERROR", "build", "ERROR: Could not find a version that satisfies the requirement numpy==1.21.0 (from versions: 1.26.0, 1.26.4)"),
            ("ERROR", "build", "ERROR: No matching distribution found for numpy==1.21.0"),
            ("ERROR", "build", "The command '/bin/sh -c pip install -r requirements.txt' returned a non-zero code: 1")],
        "dockerfile_copy_missing": [
            ("INFO", "build", "Step 6/9 : COPY dist/ /app/dist/"),
            ("ERROR", "build", v(["COPY failed: file not found in build context or excluded by .dockerignore: stat dist/: file does not exist",
                                  "failed to compute cache key: failed to calculate checksum of ref abc: \"/dist\": not found"]))],
        "port_already_in_use": [
            ("INFO", svc, "Starting HTTP server"),
            ("ERROR", svc, v(["Error: listen EADDRINUSE: address already in use :::3000",
                              "listen tcp :8080: bind: address already in use"])),
            ("WARN", "kubelet", f"Container {svc} exited with code 1")] + crash(svc, pod),
        "no_space_left": [
            ("INFO", "build", "Step 7/9 : RUN npm run build"),
            ("ERROR", "build", "ENOSPC: no space left on device, write"),
            ("ERROR", "build", "The command '/bin/sh -c npm run build' returned a non-zero code: 1")],
        "env_var_missing": boot(svc) + [
            ("ERROR", svc, v(["Missing required environment variable: STRIPE_API_KEY",
                              "KeyError: 'REDIS_URL'", "environment variable JWT_SECRET is not set"])),
            ("WARN", "kubelet", f"Container {svc} exited with code 1")] + crash(svc, pod) + lb(svc),
        "config_parse_error": boot(svc) + [
            ("ERROR", svc, v(["YAMLException: bad indentation of a mapping entry at line 14, column 3",
                              "error converting YAML to JSON: yaml: line 22: mapping values are not allowed in this context"])),
            ("WARN", "kubelet", f"Container {svc} exited with code 1")] + crash(svc, pod),
        "permission_denied": boot(svc) + [
            ("ERROR", svc, v(["Error: EACCES: permission denied, open '/app/logs/app.log'",
                              "PermissionError: [Errno 13] Permission denied: '/var/cache/app'"])),
            ("WARN", "kubelet", f"Container {svc} exited with code 1")] + crash(svc, pod),
        "tls_certificate_error": boot(svc) + [
            ("ERROR", svc, v(["Post \"https://payments.partner.example/v1/charge\": x509: certificate has expired or is not yet valid",
                              "Error: certificate has expired (CERT_HAS_EXPIRED)"])),
            ("ERROR", svc, "Startup dependency check failed, exiting")] + crash(svc, pod),
        "insufficient_cluster_resources": [
            ("INFO", "deploy", f"Applying deployment \"{svc}\" in namespace {ns}"),
            ("WARN", "default-scheduler", f"FailedScheduling pod/{pod}: 0/3 nodes are available: 3 Insufficient memory."),
            ("ERROR", "deploy", f"deployment \"{svc}\" exceeded its progress deadline")],
        "dns_resolution_failed": boot(svc) + [
            ("ERROR", svc, v([f"Error: getaddrinfo ENOTFOUND inventory-svc.{ns}", "dial tcp: lookup inventory-svc: no such host"])),
            ("ERROR", svc, "Failed to reach dependency, exiting")] + crash(svc, pod),
        "ci_tests_failed": [
            ("INFO", "ci", "Run npm test"),
            ("ERROR", "ci", "FAIL src/cart/total.test.ts"),
            ("ERROR", "ci", "AssertionError: expected 118.5 to equal 120"),
            ("ERROR", "ci", "Tests: 2 failed, 148 passed, 150 total"),
            ("ERROR", "ci", "Process completed with exit code 1.")],
        "db_connection_refused": boot(svc) + [
            ("ERROR", f"{svc}.db", v(["Error: connect ECONNREFUSED [IP]:5432", "could not connect to server: Connection refused. Is the server running on host \"db\" and accepting TCP/IP connections on port 5432?"])),
            ("ERROR", svc, "Database unavailable, exiting")] + probe() + crash(svc, pod),
        "db_auth_failed": boot(svc) + [
            ("ERROR", f"{svc}.db", v(["FATAL: password authentication failed for user \"app\"", "Access denied for user 'app'@'10.2.3.4' (using password: YES)"])),
            ("ERROR", svc, "Could not connect to database, exiting")] + crash(svc, pod),
        "db_database_missing": boot(svc) + [
            ("ERROR", f"{svc}.db", v(["FATAL: database \"ledger_prod\" does not exist", "Unknown database 'ledger_prod'"])),
            ("ERROR", svc, "Database initialization failed")] + crash(svc, pod),
        "db_migration_missing": boot(svc) + [
            ("INFO", svc, "HTTP server listening on port 8080"),
            ("ERROR", svc, v(["query failed: relation \"order_refunds\" does not exist", "SQLSTATE[42S02]: Table 'shop.order_refunds' doesn't exist"])),
            ("ERROR", svc, "GET /orders/refunds 500")] + lb(svc),
        "db_timeout": boot(svc) + [
            ("ERROR", f"{svc}.db", v(["connect ETIMEDOUT [IP]:5432 to postgres", "SequelizeConnectionError: connect ETIMEDOUT"])),
            ("ERROR", svc, "Database unavailable, exiting")] + crash(svc, pod),
        "db_too_many_connections": boot(svc) + [
            ("INFO", svc, "Scaled to 12 replicas"),
            ("ERROR", f"{svc}.db", v(["FATAL: sorry, too many clients already", "FATAL: remaining connection slots are reserved for non-replication superuser connections"])),
            ("ERROR", svc, "GET /search 500")],
        "db_tls_required": boot(svc) + [
            ("ERROR", f"{svc}.db", "FATAL: no pg_hba.conf entry for host \"10.1.2.3\", user \"app\", database \"core\", no encryption"),
            ("ERROR", svc, "Could not connect to postgres, exiting")] + crash(svc, pod),
    }


NOISE = [("INFO", "app", "GET /health 200"), ("INFO", "app", "Cache warmup complete"),
         ("DEBUG", "app", "gc cycle finished"), ("INFO", "kubelet", "Successfully pulled image in 2.1s")]


def render(lines, style, start):
    t, out = start, []
    for lvl, src, msg in lines:
        t += timedelta(milliseconds=random.randint(80, 4000))
        ts = t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"
        if style == "json":
            out.append(json.dumps({"time": ts, "level": lvl.lower(), "service": src, "msg": msg}))
        elif style == "plain":
            out.append(f"{lvl}: {msg}")
        else:
            out.append(f"{ts} {lvl} [{src}] {msg}")
    return "\n".join(out) + "\n"


def main(variants: int = 2, seed: int = 7):
    random.seed(seed)
    OUT.mkdir(parents=True, exist_ok=True)
    labels = []
    for i in range(variants):
        svc, ns = random.choice(SERVICES), random.choice(NAMESPACES)
        pod = f"{svc}-{random.randbytes(5).hex()[:10]}-{random.randbytes(3).hex()[:5]}"
        for kb_id, lines in scenarios(svc, ns, pod).items():
            lines = list(lines)
            for _ in range(random.randint(1, 3)):
                lines.insert(random.randint(0, max(0, len(lines) // 2)), random.choice(NOISE))
            style = ["iso", "json", "plain"][(i + len(kb_id)) % 3]
            name = f"{kb_id}__{i + 1}.log"
            (OUT / name).write_text(render(lines, style, datetime(2026, 10, 1, 9, tzinfo=timezone.utc)))
            labels.append({"file": name, "expected": kb_id, "style": style})
    (OUT / "labels.json").write_text(json.dumps(labels, indent=1))
    print(f"Wrote {len(labels)} logs to {OUT}")


if __name__ == "__main__":
    main()
